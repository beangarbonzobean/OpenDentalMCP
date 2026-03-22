#!/usr/bin/env python3
"""
Test database connection configuration
Helps verify database setup is correct
"""

import sys
import os
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from mcp_tools import OpenDentalMCPTools
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / '.env')

def test_db_connection():
    """Test database connection and configuration"""
    tools = OpenDentalMCPTools()
    
    print("=" * 60)
    print("DATABASE CONNECTION TEST")
    print("=" * 60)
    
    # Check configuration
    print("\n1. Checking Configuration...")
    print(f"   DB Type: {tools.db_type or 'NOT SET'}")
    print(f"   DB Server: {tools.db_server or 'NOT SET'}")
    print(f"   DB Database: {tools.db_database or 'NOT SET'}")
    print(f"   DB Username: {tools.db_username or 'NOT SET'}")
    print(f"   DB Password: {'***' if tools.db_password else 'NOT SET'}")
    print(f"   Windows Auth: {tools.db_use_windows_auth}")
    print(f"   AtoZ Path: {tools.atoz_path or 'NOT SET'}")
    
    if not tools.db_server or not tools.db_database:
        print("\n   [ERROR] Database configuration incomplete!")
        print("   Please add OPENDENTAL_DB_* variables to your .env file")
        print("   See DB_SETUP_GUIDE.md for instructions")
        return False
    
    print("\n   [OK] Configuration found")
    
    # Check database libraries
    print("\n2. Checking Database Libraries...")
    try:
        import pyodbc
        print("   [OK] pyodbc installed (for SQL Server)")
    except ImportError:
        print("   [WARNING] pyodbc not installed (needed for SQL Server)")
        print("   Install with: pip install pyodbc")
    
    try:
        import pymysql
        print("   [OK] pymysql installed (for MySQL)")
    except ImportError:
        print("   [WARNING] pymysql not installed (needed for MySQL)")
        print("   Install with: pip install pymysql")
    
    # Test connection
    print("\n3. Testing Database Connection...")
    conn = tools._get_db_connection()
    
    if conn:
        print("   [OK] Database connection successful!")
        
        # Test a simple query
        print("\n4. Testing Simple Query...")
        try:
            cursor = conn.cursor()
            
            if tools.db_type == "sqlserver":
                cursor.execute("SELECT TOP 1 PatNum, FName, LName FROM patient")
            else:  # MySQL
                cursor.execute("SELECT PatNum, FName, LName FROM patient LIMIT 1")
            
            row = cursor.fetchone()
            if row:
                print(f"   [OK] Query executed successfully")
                print(f"   Sample patient: PatNum={row[0]}, Name={row[1]} {row[2]}")
            else:
                print("   [WARNING] Query executed but returned no results")
            
            cursor.close()
            conn.close()
            
            print("\n" + "=" * 60)
            print("SUCCESS!")
            print("=" * 60)
            print("Your database connection is working correctly!")
            print("\nYou can now use:")
            print("  - query_database tool for SQL queries")
            print("  - upload_document tool for document uploads")
            print("  - create_document tool for document creation")
            
            return True
            
        except Exception as query_error:
            print(f"   [ERROR] Query failed: {query_error}")
            print("\n   Possible issues:")
            print("   - Table name might be different (check schema)")
            print("   - User might not have SELECT permissions")
            print("   - Database name might be incorrect")
            conn.close()
            return False
    else:
        print("   [ERROR] Failed to connect to database")
        print("\n   Possible issues:")
        print("   - Server name is incorrect")
        print("   - Database name is incorrect")
        print("   - Username/password is incorrect")
        print("   - Database server is not accessible")
        print("   - Firewall blocking connection")
        print("   - SQL Server/MySQL service not running")
        print("\n   Check your configuration in .env file")
        return False

if __name__ == "__main__":
    success = test_db_connection()
    sys.exit(0 if success else 1)


