import os
import boto3
from dotenv import load_dotenv

# ==========================================================
# LOGGING
# ==========================================================
from utils.logger_config import setup_logger
logger = setup_logger("S3-Object-store-cred-logs")

load_dotenv()

def get_credentials(mode: str):
    """
    mode = "read" or "write"
    Loads credentials from .env only.
    """
    if mode not in ("read", "write"):
        raise ValueError("Mode must be 'read' or 'write'.")

    access_key = os.getenv(f"{mode.upper()}_ACCESS_KEY_ID")
    secret_key = os.getenv(f"{mode.upper()}_SECRET_ACCESS_KEY")

    if not access_key or not secret_key:
        logger.error(f"Missing {mode} credentials in .env")
        raise Exception(f"Missing {mode} credentials in .env")

    creds = {
        "aws_access_key_id": access_key,
        "aws_secret_access_key": secret_key,
        "region_name": os.getenv("REGION", "us-east-1"),
        "bucket": os.getenv("BUCKET_NAME"),
        "endpoint_url": f"https://{os.getenv('HOST', 's3.amazonaws.com')}"
    }

    if not creds["bucket"]:
        logger.error("BUCKET_NAME missing in .env")
        raise Exception("BUCKET_NAME missing in .env")

    return creds


def create_client(creds: dict):
    """Create S3 client using .env credentials."""
    return boto3.client(
        "s3",
        aws_access_key_id=creds["aws_access_key_id"],
        aws_secret_access_key=creds["aws_secret_access_key"],
        region_name=creds["region_name"],
        endpoint_url=creds["endpoint_url"],
    )


def upload(local_file, object_key):
    creds = get_credentials("write")
    s3 = create_client(creds)
    bucket = creds["bucket"]

    s3.upload_file(local_file, bucket, object_key)
    logger.info(f"Uploaded {local_file} → s3://{bucket}/{object_key}")
    print(f"Uploaded {local_file} → s3://{bucket}/{object_key}")

def delete(object_key):
    """Delete a file from Object Store"""
    creds = get_credentials("write")
    s3 = create_client(creds)
    bucket = creds["bucket"]

    s3.delete_object(Bucket=bucket, Key=object_key)
    logger.info(f"Deleted s3://{bucket}/{object_key}")
    print(f"Deleted s3://{bucket}/{object_key}")

def delete_folder(prefix):
    """Delete an entire folder (all objects under a prefix)."""

    creds = get_credentials("write")
    s3 = create_client(creds)
    bucket = creds["bucket"]

    # Ensure the prefix ends with "/"
    if not prefix.endswith("/"):
        prefix = prefix + "/"
    logger.info(f"Deleting folder: {prefix}")
    print(f"Deleting folder: {prefix}")

    continuation_token = None
    total_deleted = 0

    while True:
        list_params = {
            "Bucket": bucket,
            "Prefix": prefix
        }

        if continuation_token:
            list_params["ContinuationToken"] = continuation_token

        response = s3.list_objects_v2(**list_params)

        if "Contents" not in response:
            print("No objects found inside folder.")
            break

        objects = [{"Key": obj["Key"]} for obj in response["Contents"]]

        s3.delete_objects(
            Bucket=bucket,
            Delete={"Objects": objects}
        )

        total_deleted += len(objects)

        if response.get("IsTruncated"):
            continuation_token = response["NextContinuationToken"]
        else:
            break
    logger.info(f"Deleted {total_deleted} object(s) under {prefix}")
    print(f"Deleted {total_deleted} object(s) under {prefix}")


def list_objects(prefix=None):
    creds = get_credentials("read")
    s3 = create_client(creds)
    bucket = creds["bucket"]

    params = {"Bucket": bucket}
    if prefix:
        params["Prefix"] = prefix

    response = s3.list_objects_v2(**params)

    files = []
    for item in response.get("Contents", []):
        files.append({
            "key": item["Key"],
            "size": item["Size"],
            "last_modified": item["LastModified"]
        })

    return files


def format_size(size_bytes):
    """Convert bytes to KB or MB for nicer display."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.2f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.2f} MB"


def print_table(data):
    """Pretty table output in KB/MB; no tree structure."""
    if not data:
        logger.info("No objects found.")
        print("No objects found.")
        return

    # Determine column widths
    key_width = max(len("Object Key"), max(len(item["key"]) for item in data))
    size_width = len("Size")

    # Print header
    print(f"{'Object Key'.ljust(key_width)} | {'Size'.ljust(size_width)}")
    print("-" * (key_width + size_width + 3))

    # Print rows
    for item in data:
        fmt_size = format_size(item["size"])
        print(f"{item['key'].ljust(key_width)} | {fmt_size}")

def upload_stream(file_obj, object_key):
    """Upload a file-like object (stream) to Object Store."""
    creds = get_credentials("write")
    s3 = create_client(creds)
    bucket = creds["bucket"]
    print("file_obj",file_obj)
    s3.upload_fileobj(file_obj, bucket, object_key)
    logger.info(f"Uploaded stream → s3://{bucket}/{object_key}")
    print(f"Uploaded stream → s3://{bucket}/{object_key}")

def read_file_content(object_key: str) -> str:
    """Read file content from S3/Object Store as string"""
    creds = get_credentials("read")
    s3 = create_client(creds)
    bucket = creds["bucket"]
    
    try:
        response = s3.get_object(Bucket=bucket, Key=object_key)
        content = response['Body'].read().decode('utf-8')
        logger.info(f"✅ Read content from: {object_key}")
        return content
    except Exception as e:
        logger.error(f"❌ Error reading {object_key}: {str(e)}")
        raise