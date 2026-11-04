"""Ownership records: one item per sandbox, partition key `sub` (the Cognito user), sort key `id` (the
MicroVM ID). Every lookup is keyed by the caller's sub, so another user's sandbox is simply not found.
DynamoDB in production; an in-memory dict when TABLE_NAME is unset (local development only)."""

from __future__ import annotations

import threading
from decimal import Decimal
from typing import Protocol

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config


def _plain(item: dict) -> dict:
    out = {}
    for k, v in item.items():
        if isinstance(v, Decimal):
            v = int(v) if v == v.to_integral_value() else float(v)
        out[k] = v
    return out


class Store(Protocol):
    def list(self, sub: str) -> list[dict]: ...
    def get(self, sub: str, vm_id: str) -> dict | None: ...
    def put(self, item: dict) -> None: ...
    def update(self, sub: str, vm_id: str, **fields) -> None: ...
    def delete(self, sub: str, vm_id: str) -> None: ...


class MemoryStore:
    def __init__(self):
        self._items: dict[tuple[str, str], dict] = {}
        self._lock = threading.Lock()

    def list(self, sub: str) -> list[dict]:
        with self._lock:
            return sorted((dict(v) for (s, _), v in self._items.items() if s == sub), key=lambda i: i.get("created_at", ""))

    def get(self, sub: str, vm_id: str) -> dict | None:
        with self._lock:
            v = self._items.get((sub, vm_id))
            return dict(v) if v else None

    def put(self, item: dict) -> None:
        with self._lock:
            self._items[(item["sub"], item["id"])] = dict(item)

    def update(self, sub: str, vm_id: str, **fields) -> None:
        with self._lock:
            if (sub, vm_id) in self._items:
                self._items[(sub, vm_id)].update(fields)

    def delete(self, sub: str, vm_id: str) -> None:
        with self._lock:
            self._items.pop((sub, vm_id), None)


class DynamoStore:
    def __init__(self, table: str, region: str):
        self.table = boto3.resource("dynamodb", config=Config(region_name=region, retries={"mode": "standard"})).Table(table)

    def list(self, sub: str) -> list[dict]:
        items, kw = [], {"KeyConditionExpression": Key("sub").eq(sub)}
        while True:
            r = self.table.query(**kw)
            items += [_plain(i) for i in r.get("Items", [])]
            if "LastEvaluatedKey" not in r:
                return sorted(items, key=lambda i: i.get("created_at", ""))
            kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]

    def get(self, sub: str, vm_id: str) -> dict | None:
        r = self.table.get_item(Key={"sub": sub, "id": vm_id}, ConsistentRead=True)
        return _plain(r["Item"]) if "Item" in r else None

    def put(self, item: dict) -> None:
        self.table.put_item(Item={k: v for k, v in item.items() if v is not None})

    def update(self, sub: str, vm_id: str, **fields) -> None:
        if not fields:
            return
        names = {f"#f{i}": k for i, k in enumerate(fields)} | {"#pk": "sub"}
        values = {f":v{i}": v for i, v in enumerate(fields.values())}
        expr = "SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(fields)))
        try:
            self.table.update_item(Key={"sub": sub, "id": vm_id}, UpdateExpression=expr,
                                   ExpressionAttributeNames=names, ExpressionAttributeValues=values,
                                   ConditionExpression="attribute_exists(#pk)")
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            pass

    def delete(self, sub: str, vm_id: str) -> None:
        self.table.delete_item(Key={"sub": sub, "id": vm_id})
