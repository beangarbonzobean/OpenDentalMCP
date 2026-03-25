"""
DEXIS Database Query Tool
Queries the DEXIS SQL Server database to get patient information and x-ray type.
"""

import os
import sys
import json
import logging
from pathlib import Path

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)


class DEXISDatabase:
    """Class to query DEXIS SQL Server database."""
    
    def __init__(self, connection_string=None, config=None):
        self.connection_string = connection_string
        self.config = config
        self.conn = None
        
    def connect(self):
        """Connect to DEXIS database."""
        try:
            import pyodbc
            
            # Try different connection methods
            if self.connection_string:
                self.conn = pyodbc.connect(self.connection_string)
            else:
                # Try to connect to DEXIS SQL Server instance (prefer config.json; see config.example.json)
                connection_strings = [
                    'DRIVER={SQL Server};SERVER=(local)\DEXIS_DATA;DATABASE=DEXIS;Trusted_Connection=yes;',
                    'DRIVER={ODBC Driver 17 for SQL Server};SERVER=(local)\DEXIS_DATA;DATABASE=DEXIS;Trusted_Connection=yes;',
                    'DRIVER={ODBC Driver 13 for SQL Server};SERVER=(local)\DEXIS_DATA;DATABASE=DEXIS;Trusted_Connection=yes;',
                    'DRIVER={SQL Server};SERVER=(local);DATABASE=DEXIS;Trusted_Connection=yes;',
                    'DRIVER={SQL Server};SERVER=(local)\SQLEXPRESS;DATABASE=DEXIS;Trusted_Connection=yes;',
                ]
                
                # Try SQL Server authentication if password is provided in config
                if self.config and self.config.get('database'):
                    db_config = self.config['database']
                    if not db_config.get('use_windows_auth', True) and db_config.get('password'):
                        username = db_config.get('username', 'sa')
                        password = db_config.get('password', '')
                        server = db_config.get('server', '(local)\\DEXIS_DATA')
                        database = db_config.get('database', 'DEXIS')
                        
                        sql_auth_strings = [
                            f'DRIVER={{SQL Server}};SERVER={server};DATABASE={database};UID={username};PWD={password};',
                            f'DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={server};DATABASE={database};UID={username};PWD={password};',
                            f'DRIVER={{ODBC Driver 13 for SQL Server}};SERVER={server};DATABASE={database};UID={username};PWD={password};',
                        ]
                        connection_strings = sql_auth_strings + connection_strings
                
                for conn_str in connection_strings:
                    try:
                        logger.info(f"Trying connection: {conn_str[:50]}...")
                        self.conn = pyodbc.connect(conn_str, timeout=5)
                        logger.info("Connected to DEXIS database!")
                        break
                    except Exception as e:
                        logger.debug(f"Connection failed: {e}")
                        continue
                
                if not self.conn:
                    raise Exception("Could not connect to DEXIS database. Please check SQL Server configuration.")
            
            return True
            
        except ImportError:
            logger.error("pyodbc is not installed. Install it with: pip install pyodbc")
            return False
        except Exception as e:
            logger.error(f"Error connecting to database: {e}")
            return False
    
    def get_xray_info(self, file_id):
        """
        Get patient name and x-ray type for a given file ID.
        
        Args:
            file_id: The DEXIS file ID (e.g., '00055195' or 348565)
        
        Returns:
            Dictionary with patient info and x-ray type
        """
        if not self.conn:
            if not self.connect():
                return None
        
        try:
            cursor = self.conn.cursor()
            
            # Convert file ID - DEXIS file ID 00055195 (hex) = 348565 (decimal) = VisualID!
            # File: 00055195.dex -> VisualID: 348565 (hex conversion)
            if isinstance(file_id, str) and file_id.startswith('000'):
                # File ID is hex: 00055195 -> 348565 (VisualID)
                file_id_hex = int(file_id, 16)  # Hex: 00055195 -> 348565
                # Try to extract decimal ImageNum (remove leading zeros, but handle hex chars)
                file_id_str = file_id.lstrip('0')
                # Check if it contains hex characters (a-f)
                if file_id_str and all(c in '0123456789' for c in file_id_str):
                    file_id_int = int(file_id_str)
                else:
                    # Contains hex chars, use hex value as ImageNum too
                    file_id_int = file_id_hex
                logger.info(f"File ID: {file_id} -> VisualID (hex): {file_id_hex}, ImageNum (decimal): {file_id_int}")
            elif isinstance(file_id, str):
                # Plain decimal VisualIDs from MCP (e.g., "363292") must stay decimal.
                # Only use hex interpretation for true hex-like IDs.
                file_id_clean = file_id.strip()
                if file_id_clean.isdigit():
                    file_id_hex = int(file_id_clean)
                    file_id_int = int(file_id_clean)
                else:
                    try:
                        file_id_hex = int(file_id_clean, 16)
                    except ValueError:
                        file_id_hex = int(file_id_clean)
                    file_id_int = file_id_hex
            else:
                file_id_hex = file_id
                file_id_int = file_id
            
            # Try VisualID first (hex value), then ImageNum (decimal)
            search_values = [file_id_hex]  # VisualID (most likely)
            if isinstance(file_id, str) and file_id.startswith('000'):
                file_id_str = file_id.lstrip('0')
                if file_id_str and all(c in '0123456789' for c in file_id_str):
                    search_values.append(int(file_id_str))  # ImageNum (decimal)
                else:
                    # Contains hex chars, use hex value
                    search_values.append(file_id_hex)
            
            logger.info(f"Querying database for file ID: {file_id} (trying VisualID: {file_id_hex}, ImageNum: {search_values[1] if len(search_values) > 1 else 'N/A'})")
            
            # DEXIS table structure from query3.rpt:
            # Image: VisualID, UID, ImageNum, SecurityHash
            # Patient: PersonID, DateOfBirth, etc.
            # PersonName: PersonID, TypeID, FamilyName, GivenName, MiddleName, Title, Suffix
            # Study: StudyRecordID, PatientID, Name (x-ray type!), CreatedDate
            # Person: PersonID, SSN, Removed
            
            # Image links to Visual via VisualID, Visual links to Series via SeriesID, Series links to Study
            # File ID 00055195 = VisualID 348565
            queries = [
                # Query 1: Direct from Visual -> Series -> Study -> Patient -> Person -> PersonName (by VisualID)
                """
                SELECT TOP 1
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
                    END as PatientName,
                    -- Decode x-ray type from Settings field (CrownPos) or ImageCategory
                    -- Panoramic x-rays: ImageCategory 2, no CrownPos in Settings, empty Teeth
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
                    END as XRayType,
                    v.ImageCategory,
                    v.Settings,
                    v.Teeth,
                    s.StudyRecordID,
                    v.VisualID,
                    s.CreatedDate as StudyDate
                FROM [dbo].[Visual] v
                INNER JOIN [dbo].[Series] ser ON v.SeriesID = ser.SeriesID
                INNER JOIN [dbo].[Study] s ON ser.StudyRecordID = s.StudyRecordID
                INNER JOIN [dbo].[Patient] p ON s.PatientID = p.PersonID
                INNER JOIN [dbo].[Person] per ON p.PersonID = per.PersonID
                LEFT JOIN [dbo].[PersonName] pn ON per.PersonID = pn.PersonID AND pn.TypeID = 1
                WHERE v.VisualID = ?
                """,
                # Query 2: Image -> Visual -> Series -> Study -> Patient -> Person -> PersonName (by VisualID)
                """
                SELECT TOP 1
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
                    END as PatientName,
                    -- Decode x-ray type from Settings field (CrownPos) or ImageCategory
                    -- Panoramic x-rays: ImageCategory 2, no CrownPos in Settings, empty Teeth
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
                    END as XRayType,
                    v.ImageCategory,
                    v.Settings,
                    v.Teeth,
                    s.StudyRecordID,
                    i.ImageNum,
                    i.VisualID,
                    s.CreatedDate as StudyDate
                FROM [dbo].[Image] i
                INNER JOIN [dbo].[Visual] v ON i.VisualID = v.VisualID
                INNER JOIN [dbo].[Series] ser ON v.SeriesID = ser.SeriesID
                INNER JOIN [dbo].[Study] s ON ser.StudyRecordID = s.StudyRecordID
                INNER JOIN [dbo].[Patient] p ON s.PatientID = p.PersonID
                INNER JOIN [dbo].[Person] per ON p.PersonID = per.PersonID
                LEFT JOIN [dbo].[PersonName] pn ON per.PersonID = pn.PersonID AND pn.TypeID = 1
                WHERE i.VisualID = ?
                """,
                # Query 2: Try with ImageNum (decimal)
                """
                SELECT TOP 1
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
                    END as PatientName,
                    -- Decode x-ray type from Settings field (CrownPos) or ImageCategory
                    -- Panoramic x-rays: ImageCategory 2, no CrownPos in Settings, empty Teeth
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
                    END as XRayType,
                    v.ImageCategory,
                    v.Settings,
                    v.Teeth,
                    s.StudyRecordID,
                    i.ImageNum,
                    i.VisualID,
                    s.CreatedDate as StudyDate
                FROM [dbo].[Image] i
                INNER JOIN [dbo].[Visual] v ON i.VisualID = v.VisualID
                INNER JOIN [dbo].[Series] ser ON v.SeriesID = ser.SeriesID
                INNER JOIN [dbo].[Study] s ON ser.StudyRecordID = s.StudyRecordID
                INNER JOIN [dbo].[Patient] p ON s.PatientID = p.PersonID
                INNER JOIN [dbo].[Person] per ON p.PersonID = per.PersonID
                LEFT JOIN [dbo].[PersonName] pn ON per.PersonID = pn.PersonID AND pn.TypeID = 1
                WHERE i.ImageNum = ?
                """,
            ]
            
            result = None
            
            for query in queries:
                for search_value in search_values:
                    try:
                        cursor.execute(query, search_value)
                        row = cursor.fetchone()
                        if row:
                            # Get column names
                            columns = [column[0] for column in cursor.description]
                            result = dict(zip(columns, row))
                            logger.info(f"Found record using query {queries.index(query) + 1} with value {search_value}")
                            break
                    except Exception as e:
                        logger.debug(f"Query {queries.index(query) + 1} with value {search_value} failed: {e}")
                        continue
                if result:
                    break
            
            # If no result, try to list available tables
            if not result:
                logger.warning("No result found. Listing available tables...")
                try:
                    # Try INFORMATION_SCHEMA first
                    cursor.execute("""
                        SELECT TABLE_SCHEMA, TABLE_NAME 
                        FROM INFORMATION_SCHEMA.TABLES 
                        WHERE TABLE_TYPE = 'BASE TABLE'
                        ORDER BY TABLE_SCHEMA, TABLE_NAME
                    """)
                    tables = cursor.fetchall()
                    logger.info(f"Available tables ({len(tables)} total):")
                    for schema, table in tables:
                        logger.info(f"  - [{schema}].[{table}]")
                    
                    # Also try sys.tables (may have different permissions)
                    try:
                        cursor.execute("""
                            SELECT s.name AS SchemaName, t.name AS TableName
                            FROM sys.tables t
                            INNER JOIN sys.schemas s ON t.schema_id = s.schema_id
                            ORDER BY s.name, t.name
                        """)
                        sys_tables = cursor.fetchall()
                        if sys_tables:
                            logger.info(f"\nTables from sys.tables ({len(sys_tables)} total):")
                            for schema, table in sys_tables:
                                logger.info(f"  - [{schema}].[{table}]")
                    except Exception as e:
                        logger.debug(f"Error querying sys.tables: {e}")
                    
                    # Try to find image-related tables
                    image_tables = [t[1] for t in tables if 'image' in t[1].lower() or 'img' in t[1].lower() or 'xray' in t[1].lower() or 'x-ray' in t[1].lower()]
                    patient_tables = [t[1] for t in tables if 'patient' in t[1].lower() or 'pat' in t[1].lower()]
                    logger.info(f"Image-related tables: {image_tables}")
                    logger.info(f"Patient-related tables: {patient_tables}")
                    
                    # If we found tables, try to inspect their structure
                    if image_tables:
                        logger.info(f"\nInspecting structure of {image_tables[0]}...")
                        try:
                            cursor.execute(f"""
                                SELECT COLUMN_NAME, DATA_TYPE 
                                FROM INFORMATION_SCHEMA.COLUMNS 
                                WHERE TABLE_NAME = '{image_tables[0]}'
                                ORDER BY ORDINAL_POSITION
                            """)
                            columns = cursor.fetchall()
                            logger.info("Columns:")
                            for col in columns:
                                logger.info(f"  - {col[0]} ({col[1]})")
                        except Exception as e:
                            logger.debug(f"Error inspecting table: {e}")
                except Exception as e:
                    logger.error(f"Error listing tables: {e}")
            
            return result
            
        except Exception as e:
            logger.error(f"Error querying database: {e}")
            return None
    
    def close(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()
            logger.info("Database connection closed")


def get_xray_info_from_file(file_path, config=None):
    """Extract file ID from DEXIS file path and query database."""
    file_path = Path(file_path)
    file_name = file_path.stem  # Get filename without extension
    
    # Extract file ID (e.g., '00055195' from '00055195.dex')
    file_id = file_name
    
    logger.info(f"Extracted file ID: {file_id} from file: {file_path.name}")
    
    # Query database
    db = DEXISDatabase(config=config)
    try:
        result = db.get_xray_info(file_id)
        return result
    finally:
        db.close()


def main():
    """Main entry point."""
    # Load config if available
    config = None
    try:
        with open('config.json', 'r') as f:
            config = json.load(f)
    except FileNotFoundError:
        pass
    
    if len(sys.argv) > 1:
        file_path = sys.argv[1]
    else:
        print("Usage: python dexis_db_query.py <path-to-.dex-file>")
        sys.exit(1)
    
    print("=" * 60)
    print("DEXIS Database Query Tool")
    print("=" * 60)
    print()
    
    print(f"Querying information for: {file_path}")
    print()
    
    result = get_xray_info_from_file(file_path, config)
    
    if result:
        print("=" * 60)
        print("X-Ray Information")
        print("=" * 60)
        for key, value in result.items():
            print(f"{key}: {value}")
    else:
        print("Could not retrieve information from database.")
        print("This may be due to:")
        print("  - Database connection issues")
        print("  - Different database schema")
        print("  - File ID not found in database")
        print()
        print("Please check:")
        print("  - SQL Server is running")
        print("  - Database name is 'DEXIS'")
        print("  - You have appropriate permissions")


if __name__ == '__main__':
    main()

