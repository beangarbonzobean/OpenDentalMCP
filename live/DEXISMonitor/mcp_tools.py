"""
MCP Tools for DEXIS Database Access
Provides tools for schema discovery, query execution, and pre-built queries.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from dexis_db_query import DEXISDatabase

logger = logging.getLogger(__name__)

# Maximum result size to prevent memory issues
MAX_RESULT_ROWS = 1000
MAX_RESULT_SIZE = 10 * 1024 * 1024  # 10MB


def validate_query(query: str) -> Tuple[bool, Optional[str]]:
    """
    Validate that query is read-only (SELECT only).
    
    Args:
        query: SQL query string
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    query_upper = query.strip().upper()
    
    # Block dangerous keywords
    dangerous_keywords = [
        'DROP', 'DELETE', 'UPDATE', 'INSERT', 'ALTER', 'CREATE',
        'TRUNCATE', 'EXEC', 'EXECUTE', 'SP_', 'XP_'
    ]
    
    for keyword in dangerous_keywords:
        if keyword in query_upper:
            return False, f"Query contains forbidden keyword: {keyword}. Only SELECT queries are allowed."
    
    # Must start with SELECT
    if not query_upper.startswith('SELECT'):
        return False, "Query must start with SELECT. Only read-only queries are allowed."
    
    return True, None


def format_result(rows: List, columns: List[str]) -> str:
    """
    Format query results as JSON string.
    
    Args:
        rows: List of row tuples
        columns: List of column names
        
    Returns:
        JSON string of results
    """
    try:
        # Convert rows to dictionaries
        results = []
        for row in rows:
            row_dict = {}
            for i, col in enumerate(columns):
                value = row[i]
                # Convert datetime and other objects to strings
                if hasattr(value, 'isoformat'):
                    value = value.isoformat()
                elif value is None:
                    value = None
                row_dict[col] = value
            results.append(row_dict)
        
        return json.dumps(results, indent=2, default=str)
    except Exception as e:
        logger.error(f"Error formatting results: {e}")
        return json.dumps({"error": str(e)})


def get_db_connection(config: Optional[Dict] = None) -> Optional[DEXISDatabase]:
    """
    Get database connection.
    
    Args:
        config: Configuration dictionary
        
    Returns:
        DEXISDatabase instance or None
    """
    try:
        db = DEXISDatabase(config=config)
        if db.connect():
            return db
        return None
    except Exception as e:
        logger.error(f"Error connecting to database: {e}")
        return None


# Schema Discovery Tools

def list_tables(config: Optional[Dict] = None) -> str:
    """
    List all tables in the DEXIS database.
    
    Args:
        config: Configuration dictionary
        
    Returns:
        JSON string with list of tables
    """
    db = get_db_connection(config)
    if not db:
        return json.dumps({"error": "Could not connect to database"})
    
    try:
        cursor = db.conn.cursor()
        
        # Get all tables
        cursor.execute("""
            SELECT 
                TABLE_SCHEMA,
                TABLE_NAME
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_TYPE = 'BASE TABLE'
            ORDER BY TABLE_SCHEMA, TABLE_NAME
        """)
        
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        
        result = format_result(rows, columns)
        db.close()
        return result
        
    except Exception as e:
        logger.error(f"Error listing tables: {e}")
        db.close()
        return json.dumps({"error": str(e)})


def describe_table(table_name: str, config: Optional[Dict] = None) -> str:
    """
    Get column names, types, and structure for a table.
    
    Args:
        table_name: Name of the table
        config: Configuration dictionary
        
    Returns:
        JSON string with table structure
    """
    db = get_db_connection(config)
    if not db:
        return json.dumps({"error": "Could not connect to database"})
    
    try:
        cursor = db.conn.cursor()
        
        # Get table structure
        cursor.execute("""
            SELECT 
                COLUMN_NAME,
                DATA_TYPE,
                IS_NULLABLE,
                COLUMN_DEFAULT,
                CHARACTER_MAXIMUM_LENGTH,
                NUMERIC_PRECISION,
                NUMERIC_SCALE
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = ?
            ORDER BY ORDINAL_POSITION
        """, table_name)
        
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        
        result = format_result(rows, columns)
        db.close()
        return result
        
    except Exception as e:
        logger.error(f"Error describing table {table_name}: {e}")
        db.close()
        return json.dumps({"error": str(e)})


def list_columns(table_name: str, config: Optional[Dict] = None) -> str:
    """
    Get all columns for a specific table.
    
    Args:
        table_name: Name of the table
        config: Configuration dictionary
        
    Returns:
        JSON string with column names
    """
    db = get_db_connection(config)
    if not db:
        return json.dumps({"error": "Could not connect to database"})
    
    try:
        cursor = db.conn.cursor()
        
        cursor.execute("""
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = ?
            ORDER BY ORDINAL_POSITION
        """, table_name)
        
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        
        result = format_result(rows, columns)
        db.close()
        return result
        
    except Exception as e:
        logger.error(f"Error listing columns for {table_name}: {e}")
        db.close()
        return json.dumps({"error": str(e)})


# Query Execution Tools

def execute_query(query: str, config: Optional[Dict] = None, limit: int = MAX_RESULT_ROWS) -> str:
    """
    Execute read-only SELECT query with safety checks.
    
    Args:
        query: SQL SELECT query
        config: Configuration dictionary
        limit: Maximum number of rows to return
        
    Returns:
        JSON string with query results
    """
    # Validate query
    is_valid, error = validate_query(query)
    if not is_valid:
        return json.dumps({"error": error})
    
    db = get_db_connection(config)
    if not db:
        return json.dumps({"error": "Could not connect to database"})
    
    try:
        cursor = db.conn.cursor()
        
        # Add TOP clause if not present and limit is specified
        query_upper = query.strip().upper()
        if limit and limit < MAX_RESULT_ROWS and 'TOP' not in query_upper:
            # Insert TOP after SELECT
            query = re.sub(r'^SELECT\s+', f'SELECT TOP {limit} ', query, flags=re.IGNORECASE)
        
        cursor.execute(query)
        rows = cursor.fetchall()
        
        # Check result size
        if len(rows) > MAX_RESULT_ROWS:
            rows = rows[:MAX_RESULT_ROWS]
            logger.warning(f"Result set truncated to {MAX_RESULT_ROWS} rows")
        
        columns = [desc[0] for desc in cursor.description]
        result = format_result(rows, columns)
        
        db.close()
        return result
        
    except Exception as e:
        logger.error(f"Error executing query: {e}")
        db.close()
        return json.dumps({"error": str(e)})


# Pre-built Query Tools

def search_patient(name: str, config: Optional[Dict] = None) -> str:
    """
    Find patients by name (first, last, or full name).
    
    Args:
        name: Patient name to search for
        config: Configuration dictionary
        
    Returns:
        JSON string with matching patients
    """
    db = get_db_connection(config)
    if not db:
        return json.dumps({"error": "Could not connect to database"})
    
    try:
        cursor = db.conn.cursor()
        
        # Search in PersonName table
        search_pattern = f"%{name}%"
        cursor.execute("""
            SELECT DISTINCT
                p.PersonID,
                pn.GivenName as FirstName,
                pn.FamilyName as LastName,
                CASE 
                    WHEN pn.Title IS NOT NULL AND pn.Title != '' THEN pn.Title + ' '
                    ELSE ''
                END +
                CASE 
                    WHEN pn.GivenName IS NOT NULL THEN pn.GivenName + ' '
                    ELSE ''
                END +
                CASE 
                    WHEN pn.MiddleName IS NOT NULL THEN pn.MiddleName + ' '
                    ELSE ''
                END +
                pn.FamilyName +
                CASE 
                    WHEN pn.Suffix IS NOT NULL AND pn.Suffix != '' THEN ' ' + pn.Suffix
                    ELSE ''
                END as PatientName
            FROM [dbo].[Person] p
            INNER JOIN [dbo].[Patient] pat ON p.PersonID = pat.PersonID
            LEFT JOIN [dbo].[PersonName] pn ON p.PersonID = pn.PersonID AND pn.TypeID = 1
            WHERE pn.GivenName LIKE ? 
               OR pn.FamilyName LIKE ?
               OR (pn.GivenName + ' ' + pn.FamilyName) LIKE ?
            ORDER BY pn.FamilyName, pn.GivenName
        """, search_pattern, search_pattern, search_pattern)
        
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        
        result = format_result(rows, columns)
        db.close()
        return result
        
    except Exception as e:
        logger.error(f"Error searching patient: {e}")
        db.close()
        return json.dumps({"error": str(e)})


def get_patient_xrays(patient_id: Optional[int] = None, patient_name: Optional[str] = None, config: Optional[Dict] = None) -> str:
    """
    Get all x-rays for a patient ID or name.
    
    Args:
        patient_id: Patient PersonID
        patient_name: Patient name (first, last, or full)
        config: Configuration dictionary
        
    """
    db = get_db_connection(config)
    if not db:
        return json.dumps({"error": "Could not connect to database"})
    
    try:
        cursor = db.conn.cursor()
        
        if patient_id:
            # Query by ID
            cursor.execute("""
                SELECT 
                    v.VisualID as ImageID,
                    CONVERT(VARCHAR(10), s.CreatedDate, 120) as ImageDate,
                    CONVERT(VARCHAR(8), s.CreatedDate, 108) as ImageTime,
                    CASE 
                        WHEN v.Settings LIKE '%CrownPos=V%' THEN 'Periapical'
                        WHEN v.Settings LIKE '%CrownPos=H%' THEN 'Bitewing'
                        WHEN v.Settings LIKE '%CrownPos=P%' THEN 'Panoramic'
                        WHEN v.ImageCategory = 2 AND (v.Settings NOT LIKE '%CrownPos%' OR v.Settings IS NULL) 
                             AND (v.Teeth IS NULL OR v.Teeth = '' OR v.Teeth = 'teeth=') THEN 'Panoramic'
                        WHEN v.ImageCategory = 2 THEN 'Intraoral Photo'
                        WHEN v.ImageCategory = 3 THEN 'Extraoral Photo'
                        WHEN v.ImageCategory = 4 THEN 'Other Image'
                        WHEN ser.Name IS NOT NULL AND ser.Name != '' AND ser.Name != 'Default Series' THEN ser.Name
                        ELSE s.Name
                    END as ImageType,
                    v.Teeth as ToothNumber,
                    s.StudyRecordID,
                    s.CreatedDate as StudyDate
                FROM [dbo].[Visual] v
                INNER JOIN [dbo].[Series] ser ON v.SeriesID = ser.SeriesID
                INNER JOIN [dbo].[Study] s ON ser.StudyRecordID = s.StudyRecordID
                INNER JOIN [dbo].[Patient] p ON s.PatientID = p.PersonID
                WHERE p.PersonID = ?
                ORDER BY s.CreatedDate DESC
            """, patient_id)
        elif patient_name:
            # Query by name
            search_pattern = f"%{patient_name}%"
            cursor.execute("""
                SELECT 
                    v.VisualID as ImageID,
                    CONVERT(VARCHAR(10), s.CreatedDate, 120) as ImageDate,
                    CONVERT(VARCHAR(8), s.CreatedDate, 108) as ImageTime,
                    CASE 
                        WHEN v.Settings LIKE '%CrownPos=V%' THEN 'Periapical'
                        WHEN v.Settings LIKE '%CrownPos=H%' THEN 'Bitewing'
                        WHEN v.Settings LIKE '%CrownPos=P%' THEN 'Panoramic'
                        WHEN v.ImageCategory = 2 AND (v.Settings NOT LIKE '%CrownPos%' OR v.Settings IS NULL) 
                             AND (v.Teeth IS NULL OR v.Teeth = '' OR v.Teeth = 'teeth=') THEN 'Panoramic'
                        WHEN v.ImageCategory = 2 THEN 'Intraoral Photo'
                        WHEN v.ImageCategory = 3 THEN 'Extraoral Photo'
                        WHEN v.ImageCategory = 4 THEN 'Other Image'
                        WHEN ser.Name IS NOT NULL AND ser.Name != '' AND ser.Name != 'Default Series' THEN ser.Name
                        ELSE s.Name
                    END as ImageType,
                    v.Teeth as ToothNumber,
                    s.StudyRecordID,
                    s.CreatedDate as StudyDate,
                    p.PersonID as PatientID
                FROM [dbo].[Visual] v
                INNER JOIN [dbo].[Series] ser ON v.SeriesID = ser.SeriesID
                INNER JOIN [dbo].[Study] s ON ser.StudyRecordID = s.StudyRecordID
                INNER JOIN [dbo].[Patient] p ON s.PatientID = p.PersonID
                INNER JOIN [dbo].[Person] per ON p.PersonID = per.PersonID
                LEFT JOIN [dbo].[PersonName] pn ON per.PersonID = pn.PersonID AND pn.TypeID = 1
                WHERE pn.GivenName LIKE ? 
                   OR pn.FamilyName LIKE ?
                   OR (pn.GivenName + ' ' + pn.FamilyName) LIKE ?
                ORDER BY s.CreatedDate DESC
            """, search_pattern, search_pattern, search_pattern)
        else:
            db.close()
            return json.dumps({"error": "Either patient_id or patient_name must be provided"})
        
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        
        result = format_result(rows, columns)
        db.close()
        return result
        
    except Exception as e:
        logger.error(f"Error getting patient x-rays: {e}")
        db.close()
        return json.dumps({"error": str(e)})


def get_xrays_by_date(target_date: str, config: Optional[Dict] = None) -> str:
    """
    Get x-rays taken on a specific date.
    
    Args:
        target_date: Date in format 'YYYY-MM-DD'
        config: Configuration dictionary
        
    Returns:
        JSON string with x-rays for that date
    """
    db = get_db_connection(config)
    if not db:
        return json.dumps({"error": "Could not connect to database"})
    
    try:
        cursor = db.conn.cursor()
        
        cursor.execute("""
            SELECT 
                v.VisualID as ImageID,
                CONVERT(VARCHAR(10), s.CreatedDate, 120) as ImageDate,
                CONVERT(VARCHAR(8), s.CreatedDate, 108) as ImageTime,
                CASE 
                    WHEN v.Settings LIKE '%CrownPos=V%' THEN 'Periapical'
                    WHEN v.Settings LIKE '%CrownPos=H%' THEN 'Bitewing'
                    WHEN v.Settings LIKE '%CrownPos=P%' THEN 'Panoramic'
                    WHEN v.ImageCategory = 2 AND (v.Settings NOT LIKE '%CrownPos%' OR v.Settings IS NULL) 
                         AND (v.Teeth IS NULL OR v.Teeth = '' OR v.Teeth = 'teeth=') THEN 'Panoramic'
                    WHEN v.ImageCategory = 2 THEN 'Intraoral Photo'
                    WHEN v.ImageCategory = 3 THEN 'Extraoral Photo'
                    WHEN v.ImageCategory = 4 THEN 'Other Image'
                    WHEN ser.Name IS NOT NULL AND ser.Name != '' AND ser.Name != 'Default Series' THEN ser.Name
                    ELSE s.Name
                END as ImageType,
                v.Teeth as ToothNumber,
                p.PersonID as PatientID,
                CASE 
                    WHEN pn.Title IS NOT NULL AND pn.Title != '' THEN pn.Title + ' '
                    ELSE ''
                END +
                CASE 
                    WHEN pn.GivenName IS NOT NULL THEN pn.GivenName + ' '
                    ELSE ''
                END +
                CASE 
                    WHEN pn.MiddleName IS NOT NULL THEN pn.MiddleName + ' '
                    ELSE ''
                END +
                pn.FamilyName +
                CASE 
                    WHEN pn.Suffix IS NOT NULL AND pn.Suffix != '' THEN ' ' + pn.Suffix
                    ELSE ''
                END as PatientName
            FROM [dbo].[Visual] v
            INNER JOIN [dbo].[Series] ser ON v.SeriesID = ser.SeriesID
            INNER JOIN [dbo].[Study] s ON ser.StudyRecordID = s.StudyRecordID
            INNER JOIN [dbo].[Patient] p ON s.PatientID = p.PersonID
            INNER JOIN [dbo].[Person] per ON p.PersonID = per.PersonID
            LEFT JOIN [dbo].[PersonName] pn ON per.PersonID = pn.PersonID AND pn.TypeID = 1
            WHERE CONVERT(VARCHAR(10), s.CreatedDate, 120) = ?
            ORDER BY s.CreatedDate DESC
        """, target_date)
        
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        
        result = format_result(rows, columns)
        db.close()
        return result
        
    except Exception as e:
        logger.error(f"Error getting x-rays by date: {e}")
        db.close()
        return json.dumps({"error": str(e)})


def get_xrays_by_type(xray_type: str, config: Optional[Dict] = None, limit: int = 100) -> str:
    """
    Get x-rays by type (Periapical, Bitewing, Panoramic, etc.).
    
    Args:
        xray_type: Type of x-ray (Periapical, Bitewing, Panoramic, Intraoral Photo, Extraoral Photo)
        config: Configuration dictionary
        limit: Maximum number of results
        
    Returns:
        JSON string with x-rays of that type
    """
    db = get_db_connection(config)
    if not db:
        return json.dumps({"error": "Could not connect to database"})
    
    try:
        cursor = db.conn.cursor()
        
        # Map xray_type to query conditions
        type_conditions = {
            'Periapical': "v.Settings LIKE '%CrownPos=V%'",
            'Bitewing': "v.Settings LIKE '%CrownPos=H%'",
            'Panoramic': "(v.Settings LIKE '%CrownPos=P%' OR (v.ImageCategory = 2 AND (v.Settings NOT LIKE '%CrownPos%' OR v.Settings IS NULL) AND (v.Teeth IS NULL OR v.Teeth = '' OR v.Teeth = 'teeth=')))",
            'Intraoral Photo': "v.ImageCategory = 2 AND v.Settings LIKE '%CrownPos%'",
            'Extraoral Photo': "v.ImageCategory = 3"
        }
        
        condition = type_conditions.get(xray_type, "1=0")  # Default to no results if type not found
        
        cursor.execute(f"""
            SELECT TOP {limit}
                v.VisualID as ImageID,
                CONVERT(VARCHAR(10), s.CreatedDate, 120) as ImageDate,
                CONVERT(VARCHAR(8), s.CreatedDate, 108) as ImageTime,
                CASE 
                    WHEN v.Settings LIKE '%CrownPos=V%' THEN 'Periapical'
                    WHEN v.Settings LIKE '%CrownPos=H%' THEN 'Bitewing'
                    WHEN v.Settings LIKE '%CrownPos=P%' THEN 'Panoramic'
                    WHEN v.ImageCategory = 2 AND (v.Settings NOT LIKE '%CrownPos%' OR v.Settings IS NULL) 
                         AND (v.Teeth IS NULL OR v.Teeth = '' OR v.Teeth = 'teeth=') THEN 'Panoramic'
                    WHEN v.ImageCategory = 2 THEN 'Intraoral Photo'
                    WHEN v.ImageCategory = 3 THEN 'Extraoral Photo'
                    WHEN v.ImageCategory = 4 THEN 'Other Image'
                    WHEN ser.Name IS NOT NULL AND ser.Name != '' AND ser.Name != 'Default Series' THEN ser.Name
                    ELSE s.Name
                END as ImageType,
                v.Teeth as ToothNumber,
                p.PersonID as PatientID,
                CASE 
                    WHEN pn.Title IS NOT NULL AND pn.Title != '' THEN pn.Title + ' '
                    ELSE ''
                END +
                CASE 
                    WHEN pn.GivenName IS NOT NULL THEN pn.GivenName + ' '
                    ELSE ''
                END +
                CASE 
                    WHEN pn.MiddleName IS NOT NULL THEN pn.MiddleName + ' '
                    ELSE ''
                END +
                pn.FamilyName +
                CASE 
                    WHEN pn.Suffix IS NOT NULL AND pn.Suffix != '' THEN ' ' + pn.Suffix
                    ELSE ''
                END as PatientName
            FROM [dbo].[Visual] v
            INNER JOIN [dbo].[Series] ser ON v.SeriesID = ser.SeriesID
            INNER JOIN [dbo].[Study] s ON ser.StudyRecordID = s.StudyRecordID
            INNER JOIN [dbo].[Patient] p ON s.PatientID = p.PersonID
            INNER JOIN [dbo].[Person] per ON p.PersonID = per.PersonID
            LEFT JOIN [dbo].[PersonName] pn ON per.PersonID = pn.PersonID AND pn.TypeID = 1
            WHERE {condition}
            ORDER BY s.CreatedDate DESC
        """)
        
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        
        result = format_result(rows, columns)
        db.close()
        return result
        
    except Exception as e:
        logger.error(f"Error getting x-rays by type: {e}")
        db.close()
        return json.dumps({"error": str(e)})


def get_xray_info(visual_id: int, config: Optional[Dict] = None) -> str:
    """
    Get detailed info for a specific x-ray by VisualID.
    
    Args:
        visual_id: VisualID of the x-ray
        config: Configuration dictionary
        
    Returns:
        JSON string with x-ray details
    """
    db = get_db_connection(config)
    if not db:
        return json.dumps({"error": "Could not connect to database"})
    
    try:
        # Reuse existing get_xray_info method
        result = db.get_xray_info(str(visual_id))
        db.close()
        
        if result:
            return json.dumps(result, indent=2, default=str)
        else:
            return json.dumps({"error": "X-ray not found"})
            
    except Exception as e:
        logger.error(f"Error getting x-ray info: {e}")
        db.close()
        return json.dumps({"error": str(e)})


def get_recent_xrays(limit: int = 50, config: Optional[Dict] = None) -> str:
    """
    Get most recent x-rays.
    
    Args:
        limit: Maximum number of x-rays to return
        config: Configuration dictionary
        
    Returns:
        JSON string with recent x-rays
    """
    db = get_db_connection(config)
    if not db:
        return json.dumps({"error": "Could not connect to database"})
    
    try:
        cursor = db.conn.cursor()
        
        cursor.execute(f"""
            SELECT TOP {limit}
                v.VisualID as ImageID,
                CONVERT(VARCHAR(10), s.CreatedDate, 120) as ImageDate,
                CONVERT(VARCHAR(8), s.CreatedDate, 108) as ImageTime,
                CASE 
                    WHEN v.Settings LIKE '%CrownPos=V%' THEN 'Periapical'
                    WHEN v.Settings LIKE '%CrownPos=H%' THEN 'Bitewing'
                    WHEN v.Settings LIKE '%CrownPos=P%' THEN 'Panoramic'
                    WHEN v.ImageCategory = 2 AND (v.Settings NOT LIKE '%CrownPos%' OR v.Settings IS NULL) 
                         AND (v.Teeth IS NULL OR v.Teeth = '' OR v.Teeth = 'teeth=') THEN 'Panoramic'
                    WHEN v.ImageCategory = 2 THEN 'Intraoral Photo'
                    WHEN v.ImageCategory = 3 THEN 'Extraoral Photo'
                    WHEN v.ImageCategory = 4 THEN 'Other Image'
                    WHEN ser.Name IS NOT NULL AND ser.Name != '' AND ser.Name != 'Default Series' THEN ser.Name
                    ELSE s.Name
                END as ImageType,
                v.Teeth as ToothNumber,
                p.PersonID as PatientID,
                CASE 
                    WHEN pn.Title IS NOT NULL AND pn.Title != '' THEN pn.Title + ' '
                    ELSE ''
                END +
                CASE 
                    WHEN pn.GivenName IS NOT NULL THEN pn.GivenName + ' '
                    ELSE ''
                END +
                CASE 
                    WHEN pn.MiddleName IS NOT NULL THEN pn.MiddleName + ' '
                    ELSE ''
                END +
                pn.FamilyName +
                CASE 
                    WHEN pn.Suffix IS NOT NULL AND pn.Suffix != '' THEN ' ' + pn.Suffix
                    ELSE ''
                END as PatientName
            FROM [dbo].[Visual] v
            INNER JOIN [dbo].[Series] ser ON v.SeriesID = ser.SeriesID
            INNER JOIN [dbo].[Study] s ON ser.StudyRecordID = s.StudyRecordID
            INNER JOIN [dbo].[Patient] p ON s.PatientID = p.PersonID
            INNER JOIN [dbo].[Person] per ON p.PersonID = per.PersonID
            LEFT JOIN [dbo].[PersonName] pn ON per.PersonID = pn.PersonID AND pn.TypeID = 1
            ORDER BY s.CreatedDate DESC
        """)
        
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        
        result = format_result(rows, columns)
        db.close()
        return result
        
    except Exception as e:
        logger.error(f"Error getting recent x-rays: {e}")
        db.close()
        return json.dumps({"error": str(e)})


def search_xrays(
    patient_name: Optional[str] = None,
    xray_type: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 100,
    config: Optional[Dict] = None
) -> str:
    """
    Advanced search with multiple filters.
    
    Args:
        patient_name: Patient name filter
        xray_type: X-ray type filter
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        limit: Maximum number of results
        config: Configuration dictionary
        
    Returns:
        JSON string with matching x-rays
    """
    db = get_db_connection(config)
    if not db:
        return json.dumps({"error": "Could not connect to database"})
    
    try:
        cursor = db.conn.cursor()
        
        # Build WHERE clause dynamically
        where_conditions = []
        params = []
        
        if patient_name:
            search_pattern = f"%{patient_name}%"
            where_conditions.append("(pn.GivenName LIKE ? OR pn.FamilyName LIKE ? OR (pn.GivenName + ' ' + pn.FamilyName) LIKE ?)")
            params.extend([search_pattern, search_pattern, search_pattern])
        
        if xray_type:
            type_conditions = {
                'Periapical': "v.Settings LIKE '%CrownPos=V%'",
                'Bitewing': "v.Settings LIKE '%CrownPos=H%'",
                'Panoramic': "(v.Settings LIKE '%CrownPos=P%' OR (v.ImageCategory = 2 AND (v.Settings NOT LIKE '%CrownPos%' OR v.Settings IS NULL) AND (v.Teeth IS NULL OR v.Teeth = '' OR v.Teeth = 'teeth=')))",
                'Intraoral Photo': "v.ImageCategory = 2 AND v.Settings LIKE '%CrownPos%'",
                'Extraoral Photo': "v.ImageCategory = 3"
            }
            condition = type_conditions.get(xray_type)
            if condition:
                where_conditions.append(f"({condition})")
        
        if start_date:
            where_conditions.append("CONVERT(VARCHAR(10), s.CreatedDate, 120) >= ?")
            params.append(start_date)
        
        if end_date:
            where_conditions.append("CONVERT(VARCHAR(10), s.CreatedDate, 120) <= ?")
            params.append(end_date)
        
        where_clause = " AND ".join(where_conditions) if where_conditions else "1=1"
        
        query = f"""
            SELECT TOP {limit}
                v.VisualID as ImageID,
                CONVERT(VARCHAR(10), s.CreatedDate, 120) as ImageDate,
                CONVERT(VARCHAR(8), s.CreatedDate, 108) as ImageTime,
                CASE 
                    WHEN v.Settings LIKE '%CrownPos=V%' THEN 'Periapical'
                    WHEN v.Settings LIKE '%CrownPos=H%' THEN 'Bitewing'
                    WHEN v.Settings LIKE '%CrownPos=P%' THEN 'Panoramic'
                    WHEN v.ImageCategory = 2 AND (v.Settings NOT LIKE '%CrownPos%' OR v.Settings IS NULL) 
                         AND (v.Teeth IS NULL OR v.Teeth = '' OR v.Teeth = 'teeth=') THEN 'Panoramic'
                    WHEN v.ImageCategory = 2 THEN 'Intraoral Photo'
                    WHEN v.ImageCategory = 3 THEN 'Extraoral Photo'
                    WHEN v.ImageCategory = 4 THEN 'Other Image'
                    WHEN ser.Name IS NOT NULL AND ser.Name != '' AND ser.Name != 'Default Series' THEN ser.Name
                    ELSE s.Name
                END as ImageType,
                v.Teeth as ToothNumber,
                p.PersonID as PatientID,
                CASE 
                    WHEN pn.Title IS NOT NULL AND pn.Title != '' THEN pn.Title + ' '
                    ELSE ''
                END +
                CASE 
                    WHEN pn.GivenName IS NOT NULL THEN pn.GivenName + ' '
                    ELSE ''
                END +
                CASE 
                    WHEN pn.MiddleName IS NOT NULL THEN pn.MiddleName + ' '
                    ELSE ''
                END +
                pn.FamilyName +
                CASE 
                    WHEN pn.Suffix IS NOT NULL AND pn.Suffix != '' THEN ' ' + pn.Suffix
                    ELSE ''
                END as PatientName
            FROM [dbo].[Visual] v
            INNER JOIN [dbo].[Series] ser ON v.SeriesID = ser.SeriesID
            INNER JOIN [dbo].[Study] s ON ser.StudyRecordID = s.StudyRecordID
            INNER JOIN [dbo].[Patient] p ON s.PatientID = p.PersonID
            INNER JOIN [dbo].[Person] per ON p.PersonID = per.PersonID
            LEFT JOIN [dbo].[PersonName] pn ON per.PersonID = pn.PersonID AND pn.TypeID = 1
            WHERE {where_clause}
            ORDER BY s.CreatedDate DESC
        """
        
        cursor.execute(query, params)
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        
        result = format_result(rows, columns)
        db.close()
        return result
        
    except Exception as e:
        logger.error(f"Error searching x-rays: {e}")
        db.close()
        return json.dumps({"error": str(e)})


def get_xray_statistics(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    config: Optional[Dict] = None
) -> str:
    """
    Get statistics (counts by type, date ranges, etc.).
    
    Args:
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        config: Configuration dictionary
        
    Returns:
        JSON string with statistics
    """
    db = get_db_connection(config)
    if not db:
        return json.dumps({"error": "Could not connect to database"})
    
    try:
        cursor = db.conn.cursor()
        
        # Build date filter
        date_filter = ""
        params = []
        if start_date and end_date:
            date_filter = "WHERE CONVERT(VARCHAR(10), s.CreatedDate, 120) BETWEEN ? AND ?"
            params = [start_date, end_date]
        elif start_date:
            date_filter = "WHERE CONVERT(VARCHAR(10), s.CreatedDate, 120) >= ?"
            params = [start_date]
        elif end_date:
            date_filter = "WHERE CONVERT(VARCHAR(10), s.CreatedDate, 120) <= ?"
            params = [end_date]
        
        # Get counts by type
        cursor.execute(f"""
            SELECT 
                CASE 
                    WHEN v.Settings LIKE '%CrownPos=V%' THEN 'Periapical'
                    WHEN v.Settings LIKE '%CrownPos=H%' THEN 'Bitewing'
                    WHEN v.Settings LIKE '%CrownPos=P%' THEN 'Panoramic'
                    WHEN v.ImageCategory = 2 AND (v.Settings NOT LIKE '%CrownPos%' OR v.Settings IS NULL) 
                         AND (v.Teeth IS NULL OR v.Teeth = '' OR v.Teeth = 'teeth=') THEN 'Panoramic'
                    WHEN v.ImageCategory = 2 THEN 'Intraoral Photo'
                    WHEN v.ImageCategory = 3 THEN 'Extraoral Photo'
                    WHEN v.ImageCategory = 4 THEN 'Other Image'
                    ELSE 'Unknown'
                END as ImageType,
                COUNT(*) as Count
            FROM [dbo].[Visual] v
            INNER JOIN [dbo].[Series] ser ON v.SeriesID = ser.SeriesID
            INNER JOIN [dbo].[Study] s ON ser.StudyRecordID = s.StudyRecordID
            {date_filter}
            GROUP BY 
                CASE 
                    WHEN v.Settings LIKE '%CrownPos=V%' THEN 'Periapical'
                    WHEN v.Settings LIKE '%CrownPos=H%' THEN 'Bitewing'
                    WHEN v.Settings LIKE '%CrownPos=P%' THEN 'Panoramic'
                    WHEN v.ImageCategory = 2 AND (v.Settings NOT LIKE '%CrownPos%' OR v.Settings IS NULL) 
                         AND (v.Teeth IS NULL OR v.Teeth = '' OR v.Teeth = 'teeth=') THEN 'Panoramic'
                    WHEN v.ImageCategory = 2 THEN 'Intraoral Photo'
                    WHEN v.ImageCategory = 3 THEN 'Extraoral Photo'
                    WHEN v.ImageCategory = 4 THEN 'Other Image'
                    ELSE 'Unknown'
                END
            ORDER BY Count DESC
        """, *params)
        
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        
        result = format_result(rows, columns)
        db.close()
        return result
        
    except Exception as e:
        logger.error(f"Error getting x-ray statistics: {e}")
        db.close()
        return json.dumps({"error": str(e)})

