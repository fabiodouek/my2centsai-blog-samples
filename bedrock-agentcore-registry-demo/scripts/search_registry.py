#!/usr/bin/env python3
"""
Search an AWS Agent Registry using the Python SDK (boto3).

Demonstrates the control plane / data plane client split:
- bedrock-agentcore-control: CRUD operations (create, update, delete registries and records)
- bedrock-agentcore: data plane operations (search)

Usage:
    python scripts/search_registry.py <registry-id> [region]

Examples:
    python scripts/search_registry.py abcd1234efgh
    python scripts/search_registry.py abcd1234efgh us-west-2
"""

import json
import sys

import boto3


def get_registry_arn(control_client, registry_id: str) -> str:
    """Look up the full registry ARN from a registry ID."""
    response = control_client.get_registry(registryId=registry_id)
    return response["registryArn"]


def search(data_client, registry_arn: str, query: str, max_results: int = 10, filters: dict | None = None):
    """Run a search query against the registry."""
    params = {
        "registryIds": [registry_arn],
        "searchQuery": query,
        "maxResults": max_results,
    }
    if filters:
        params["filters"] = filters

    return data_client.search_registry_records(**params)


def print_results(label: str, query: str, response: dict):
    """Pretty-print search results."""
    records = response.get("registryRecords", [])
    print(f"\n{'=' * 60}")
    print(f"  {label}")
    print(f"  Query: \"{query}\"")
    print(f"  Results: {len(records)}")
    print(f"{'=' * 60}")
    for record in records:
        print(f"  - {record['name']} ({record.get('descriptorType', 'unknown')})")
        print(f"    {record.get('description', '')[:80]}")
    if not records:
        print("  (no results)")
    print()


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <registry-id> [region]")
        sys.exit(1)

    registry_id = sys.argv[1]
    region = sys.argv[2] if len(sys.argv) > 2 else "us-east-1"

    # Control plane client: for CRUD operations on registries and records
    control = boto3.client("bedrock-agentcore-control", region_name=region)

    # Data plane client: for search operations
    data = boto3.client("bedrock-agentcore", region_name=region)

    # Look up the registry ARN
    registry_arn = get_registry_arn(control, registry_id)
    print(f"Registry ARN: {registry_arn}")

    # Test 1: Keyword search
    response = search(data, registry_arn, "weather")
    print_results("Test 1: Keyword Search", "weather", response)

    # Test 2: Semantic search
    query = "I need something that handles billing and payments"
    response = search(data, registry_arn, query)
    print_results("Test 2: Semantic Search", query, response)

    # Test 3: Natural language query
    query = "find tools that help with code quality and security"
    response = search(data, registry_arn, query)
    print_results("Test 3: Natural Language Query", query, response)

    # Test 4: Filtered search (MCP servers only)
    query = "forecast"
    response = search(data, registry_arn, query, filters={"descriptorType": {"$eq": "MCP"}})
    print_results("Test 4: Filtered Search (MCP only)", query, response)

    # Test 5: Filtered search (skills only)
    query = "security review"
    response = search(data, registry_arn, query, filters={"descriptorType": {"$eq": "AGENT_SKILLS"}})
    print_results("Test 5: Filtered Search (skills only)", query, response)


if __name__ == "__main__":
    main()
