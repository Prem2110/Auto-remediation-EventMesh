"""
XSD file handling and content extraction
"""
import xml.etree.ElementTree as ET
from typing import Dict, Any
from storage.object_store import read_file_content
from utils.logger_config import setup_logger

logger = setup_logger("xsd-handler")


def read_xsd_from_storage(object_key: str) -> str:
    """
    Read XSD file content from S3/Object Store
    
    Args:
        object_key: S3 object key (e.g., "user/session-id/Invoice.xsd")
        
    Returns:
        XSD file content as string
    """
    logger.info(f"Reading XSD from: {object_key}")
    return read_file_content(object_key)


def validate_xsd_content(content: str) -> bool:
    """
    Validate if content is valid XSD
    
    Args:
        content: XSD file content as string
        
    Returns:
        True if valid XSD, False otherwise
    """
    try:
        root = ET.fromstring(content)
        # Check if it's an XSD schema
        if 'schema' in root.tag.lower():
            logger.info(" Valid XSD detected")
            return True
        logger.warning(" Not a valid XSD schema")
        return False
    except Exception as e:
        logger.error(f"❌ XSD validation failed: {str(e)}")
        return False


def extract_xsd_metadata(content: str) -> Dict[str, Any]:
    """
    Extract useful metadata from XSD content
    
    Args:
        content: XSD file content
        
    Returns:
        Dictionary with XSD metadata
    """
    try:
        root = ET.fromstring(content)
        
        # Extract namespace
        ns = {'xs': 'http://www.w3.org/2001/XMLSchema'}
        target_namespace = root.get('targetNamespace', '')
        
        # Extract root elements
        elements = []
        for elem in root.findall('.//xs:element', ns):
            elem_name = elem.get('name')
            elem_type = elem.get('type')
            if elem_name:
                elements.append({
                    'name': elem_name,
                    'type': elem_type
                })
        
        # Extract complex types
        complex_types = []
        for ct in root.findall('.//xs:complexType', ns):
            type_name = ct.get('name')
            if type_name:
                complex_types.append(type_name)
        
        metadata = {
            'target_namespace': target_namespace,
            'elements': elements,
            'complex_types': complex_types,
            'element_count': len(elements),
            'type_count': len(complex_types)
        }
        
        logger.info(f" Extracted XSD metadata: {len(elements)} elements, {len(complex_types)} types")
        return metadata
        
    except Exception as e:
        logger.error(f" XSD metadata extraction failed: {str(e)}")
        return {
            'error': f'Failed to parse XSD: {str(e)}',
            'target_namespace': '',
            'element_count': 0,
            'type_count': 0
        }