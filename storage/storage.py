# # app/storage.py - CHANGES NEEDED

# """
# File storage operations and utilities
# """
# import uuid
# import mimetypes
# from typing import List, Dict, Any
# from fastapi import UploadFile
# from app.config import settings
# from app.object_store import upload_stream  
# from app.xsd_handler import read_xsd_from_storage, validate_xsd_content, extract_xsd_metadata
# from app.database import insert_file_metadata, insert_xsd_metadata  

# # ==========================================================
# # LOGGING
# # ==========================================================
# from utils.logger_config import setup_logger
# logger = setup_logger("Object-Store")

# async def prepare_file_metadata(file: UploadFile, object_key: str, 
#                                 uploaded_by: str = "default_user") -> Dict[str, Any]:
#     """
#     Prepare file metadata for database storage
#     """
#     # Read file to calculate size
#     file_bytes = await file.read()
#     size = len(file_bytes)
    
#     # Reset file pointer for upload
#     await file.seek(0)
    
#     # Determine MIME type
#     mime_type = (
#         file.content_type or 
#         mimetypes.guess_type(file.filename)[0] or 
#         "application/octet-stream"
#     )
    
#     return {
#         "file_id": str(uuid.uuid4()),
#         "object_key": object_key,
#         "file_name": file.filename,
#         "mime_type": mime_type,
#         "size": size,
#         "uploaded_by": uploaded_by
#     }


# async def upload_multiple_files(session_id: str, files: List[UploadFile], 
#                                 timestamp: str, user_id: str) -> Dict[str, List[Dict[str, Any]]]:
#     """
#     Upload multiple files and store their metadata
#     """
#     results = []
    
#     for file in files:
#         object_key = f"{settings.UPLOAD_ROOT}/{session_id}/{file.filename}".replace("\\", "/")
        
#         try:
#             # Prepare metadata
#             metadata = await prepare_file_metadata(file, object_key, user_id)
            
#             # Upload file to S3
#             upload_stream(file.file, object_key)  # ← THIS NOW WORKS
            
#             # Store metadata in database
#             insert_file_metadata(session_id, metadata, timestamp)
            
#             results.append({
#                 "file_name": file.filename,
#                 "file_id": metadata["file_id"],
#                 "status": "success"
#             })
            
#         except Exception as e:
#             results.append({
#                 "file_name": file.filename,
#                 "error": str(e),
#                 "status": "failed"
#             })
    
#     return {"uploaded_files": results}

"""
File storage operations and utilities
"""
import uuid
import mimetypes
from typing import List, Dict, Any
from fastapi import UploadFile
from config.config import settings
from db.database import insert_file_metadata, insert_xsd_metadata
from storage.object_store import upload_stream
from utils.xsd_handler import read_xsd_from_storage, validate_xsd_content, extract_xsd_metadata

from utils.logger_config import setup_logger
logger = setup_logger("storage")


async def prepare_file_metadata(file: UploadFile, object_key: str, 
                                uploaded_by: str = "default_user") -> Dict[str, Any]:
    """Prepare file metadata for database storage"""
    # Read file to calculate size
    file_bytes = await file.read()
    size = len(file_bytes)
    
    # Reset file pointer for upload
    await file.seek(0)
    
    # Determine MIME type
    mime_type = (
        file.content_type or 
        mimetypes.guess_type(file.filename)[0] or 
        "application/octet-stream"
    )
    
    return {
        "file_id": str(uuid.uuid4()),
        "object_key": object_key,
        "file_name": file.filename,
        "mime_type": mime_type,
        "size": size,
        "uploaded_by": uploaded_by
    }


async def upload_multiple_files(session_id: str, files: List[UploadFile], 
                                timestamp: str, user_id: str) -> Dict[str, List[Dict[str, Any]]]:
    """
    Upload multiple files and store their metadata
    Detects and processes XSD files specially
    """
    results = []
    
    for file in files:
        object_key = f"{settings.UPLOAD_ROOT}/{session_id}/{file.filename}".replace("\\", "/")
        
        logger.info(f"Processing file: {file.filename}")
        
        try:
            # Prepare metadata
            metadata = await prepare_file_metadata(file, object_key, user_id)
            file_id = metadata["file_id"]
            
            # Upload file to S3
            logger.info(f"Uploading {file.filename} to S3...")
            upload_stream(file.file, object_key)
            
            # Store file metadata in database
            logger.info(f"Storing file metadata in HANA...")
            insert_file_metadata(session_id, metadata, timestamp)
            
            # Detect and process XSD files
            is_xsd = file.filename.lower().endswith('.xsd')
            
            if is_xsd:
                logger.info(f"XSD file detected: {file.filename}")
                
                try:
                    # Read XSD content from S3
                    logger.info(f"Reading XSD content from S3...")
                    xsd_content = read_xsd_from_storage(object_key)
                    
                    # Validate XSD
                    if validate_xsd_content(xsd_content):
                        logger.info(f" XSD validation passed")
                        
                        # Extract metadata
                        xsd_meta = extract_xsd_metadata(xsd_content)
                        
                        # Store XSD-specific data
                        logger.info(f"Storing XSD metadata in HANA...")
                        insert_xsd_metadata(
                            session_id, 
                            file_id, 
                            xsd_content, 
                            xsd_meta, 
                            timestamp, 
                            user_id
                        )
                        
                        results.append({
                            "file_name": file.filename,
                            "file_id": file_id,
                            "file_type": "xsd",
                            "status": "success",
                            "xsd_info": {
                                "namespace": xsd_meta.get('target_namespace', 'N/A'),
                                "elements": xsd_meta.get('element_count', 0),
                                "types": xsd_meta.get('type_count', 0)
                            }
                        })
                        logger.info(f" XSD processing complete for {file.filename}")
                    else:
                        logger.warning(f" Invalid XSD format: {file.filename}")
                        results.append({
                            "file_name": file.filename,
                            "file_id": file_id,
                            "status": "warning",
                            "message": "File uploaded but XSD validation failed"
                        })
                        
                except Exception as xsd_error:
                    logger.error(f" XSD processing error for {file.filename}: {str(xsd_error)}")
                    results.append({
                        "file_name": file.filename,
                        "file_id": file_id,
                        "status": "partial_success",
                        "message": "File uploaded but XSD processing failed",
                        "error": str(xsd_error)
                    })
            else:
                # Regular file (not XSD)
                logger.info(f" Regular file uploaded: {file.filename}")
                results.append({
                    "file_name": file.filename,
                    "file_id": file_id,
                    "status": "success"
                })
            
        except Exception as e:
            logger.error(f" File upload failed for {file.filename}: {str(e)}")
            results.append({
                "file_name": file.filename,
                "error": str(e),
                "status": "failed"
            })
    
    return {"uploaded_files": results}
