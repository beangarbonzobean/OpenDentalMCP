"""
DEXIS MCP Connection Test
Tests basic connectivity and functionality of DEXIS MCP tools.
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('dexis_connection_test.log'),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)

def load_config():
    """Load configuration from config.json or config.test.json"""
    config_paths = ['config.json', 'config.test.json']

    for config_path in config_paths:
        if Path(config_path).exists():
            try:
                with open(config_path, 'r') as f:
                    config = json.load(f)
                    logger.info(f"Loaded configuration from {config_path}")
                    return config
            except Exception as e:
                logger.error(f"Error loading {config_path}: {e}")

    logger.warning("No configuration file found, using defaults")
    return None


def test_connection(config):
    """Test database connection"""
    logger.info("=" * 60)
    logger.info("Testing DEXIS database connection...")
    logger.info("=" * 60)

    try:
        from dexis_db_query import DEXISDatabase

        db = DEXISDatabase(config=config)
        if db.connect():
            logger.info("✓ Database connection successful")
            db.close()
            return True
        else:
            logger.error("✗ Database connection failed")
            return False
    except Exception as e:
        logger.error(f"✗ Connection test failed with error: {e}")
        return False


def test_list_tables(config):
    """Test listing database tables"""
    logger.info("=" * 60)
    logger.info("Testing table listing...")
    logger.info("=" * 60)

    try:
        from mcp_tools import list_tables

        result = list_tables(config)
        result_data = json.loads(result)

        if "error" in result_data:
            logger.error(f"✗ List tables failed: {result_data['error']}")
            return False
        else:
            table_count = len(result_data)
            logger.info(f"✓ Successfully listed {table_count} tables")
            if table_count > 0:
                logger.info(f"  Sample tables: {result_data[:3]}")
            return True
    except Exception as e:
        logger.error(f"✗ List tables test failed: {e}")
        return False


def test_search_patient(config):
    """Test patient search functionality"""
    logger.info("=" * 60)
    logger.info("Testing patient search...")
    logger.info("=" * 60)

    try:
        from mcp_tools import search_patient

        # Test with common name "Young" (Ben Young is the test patient per memory)
        test_name = "Young"
        logger.info(f"Searching for patient: {test_name}")

        result = search_patient(test_name, config)
        result_data = json.loads(result)

        if "error" in result_data:
            logger.error(f"✗ Patient search failed: {result_data['error']}")
            return False
        else:
            patient_count = len(result_data)
            logger.info(f"✓ Found {patient_count} patients matching '{test_name}'")
            if patient_count > 0:
                logger.info(f"  Sample result: {result_data[0]}")
            return True
    except Exception as e:
        logger.error(f"✗ Patient search test failed: {e}")
        return False


def test_recent_xrays(config):
    """Test getting recent x-rays"""
    logger.info("=" * 60)
    logger.info("Testing recent x-rays query...")
    logger.info("=" * 60)

    try:
        from mcp_tools import get_recent_xrays

        limit = 10
        logger.info(f"Getting {limit} most recent x-rays...")

        result = get_recent_xrays(limit=limit, config=config)
        result_data = json.loads(result)

        if "error" in result_data:
            logger.error(f"✗ Recent x-rays query failed: {result_data['error']}")
            return False
        else:
            xray_count = len(result_data)
            logger.info(f"✓ Retrieved {xray_count} recent x-rays")
            if xray_count > 0:
                logger.info(f"  Most recent: {result_data[0]}")
            return True
    except Exception as e:
        logger.error(f"✗ Recent x-rays test failed: {e}")
        return False


def test_xray_statistics(config):
    """Test x-ray statistics"""
    logger.info("=" * 60)
    logger.info("Testing x-ray statistics...")
    logger.info("=" * 60)

    try:
        from mcp_tools import get_xray_statistics

        logger.info("Getting x-ray statistics...")

        result = get_xray_statistics(config=config)
        result_data = json.loads(result)

        if "error" in result_data:
            logger.error(f"✗ Statistics query failed: {result_data['error']}")
            return False
        else:
            logger.info(f"✓ Retrieved statistics for {len(result_data)} x-ray types")
            for stat in result_data:
                logger.info(f"  - {stat.get('ImageType', 'Unknown')}: {stat.get('Count', 0)} images")
            return True
    except Exception as e:
        logger.error(f"✗ Statistics test failed: {e}")
        return False


def main():
    """Run all tests"""
    logger.info("=" * 80)
    logger.info(f"DEXIS MCP Connection Test - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 80)
    logger.info("")

    # Load configuration
    config = load_config()

    # Run tests
    tests = [
        ("Database Connection", test_connection),
        ("List Tables", test_list_tables),
        ("Search Patient", test_search_patient),
        ("Recent X-rays", test_recent_xrays),
        ("X-ray Statistics", test_xray_statistics),
    ]

    results = {}
    for test_name, test_func in tests:
        try:
            results[test_name] = test_func(config)
        except Exception as e:
            logger.error(f"Test '{test_name}' crashed: {e}")
            results[test_name] = False
        logger.info("")

    # Summary
    logger.info("=" * 80)
    logger.info("TEST SUMMARY")
    logger.info("=" * 80)

    passed = sum(1 for result in results.values() if result)
    total = len(results)

    for test_name, result in results.items():
        status = "✓ PASS" if result else "✗ FAIL"
        logger.info(f"{status}: {test_name}")

    logger.info("")
    logger.info(f"Results: {passed}/{total} tests passed")

    if passed == total:
        logger.info("✓ All tests passed! DEXIS MCP is functioning correctly.")
        return 0
    else:
        logger.warning(f"⚠ {total - passed} test(s) failed. Review logs for details.")
        return 1


if __name__ == '__main__':
    sys.exit(main())
