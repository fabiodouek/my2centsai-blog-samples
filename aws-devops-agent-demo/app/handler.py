import json
import os
import uuid
import time
import boto3
from botocore.exceptions import ClientError

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(os.environ["TABLE_NAME"])


def lambda_handler(event, context):
    http_method = event["httpMethod"]
    path = event.get("path", "/")

    try:
        if http_method == "GET" and path == "/items":
            return list_items()
        elif http_method == "GET" and path.startswith("/items/"):
            item_id = path.split("/")[-1]
            return get_item(item_id)
        elif http_method == "POST" and path == "/items":
            body = json.loads(event.get("body", "{}"))
            return create_item(body)
        elif http_method == "DELETE" and path.startswith("/items/"):
            item_id = path.split("/")[-1]
            return delete_item(item_id)
        elif http_method == "GET" and path == "/health":
            return response(200, {"status": "healthy", "timestamp": int(time.time())})
        else:
            return response(404, {"error": "Not found"})
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        print(f"DynamoDB error: {error_code} - {e.response['Error']['Message']}")
        return response(500, {"error": f"Database error: {error_code}"})
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        return response(500, {"error": "Internal server error"})


def list_items():
    result = table.scan(Limit=50)
    return response(200, {"items": result.get("Items", []), "count": result["Count"]})


def get_item(item_id):
    result = table.get_item(Key={"id": item_id})
    item = result.get("Item")
    if not item:
        return response(404, {"error": "Item not found"})
    return response(200, item)


def create_item(body):
    if not body.get("name"):
        return response(400, {"error": "Missing required field: name"})

    item = {
        "id": str(uuid.uuid4()),
        "name": body["name"],
        "description": body.get("description", ""),
        "created_at": int(time.time()),
    }
    table.put_item(Item=item)
    return response(201, item)


def delete_item(item_id):
    table.delete_item(Key={"id": item_id})
    return response(200, {"message": "Item deleted", "id": item_id})


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body, default=str),
    }
