"""
Recover patient table rows from MariaDB binary logs.
Parses ROW-format binlog events to extract INSERT/UPDATE for the patient table.
"""
import subprocess
import sys
import re
import os

MYSQLBINLOG = r"C:\Program Files\MariaDB 10.11\bin\mysqlbinlog.exe"
MYSQL = r"C:\Program Files\MariaDB 10.11\bin\mysql.exe"
DATADIR = r"D:\mysql\data"

# Binary log files to process (in order)
BINLOG_FILES = [
    os.path.join(DATADIR, f"mysql-bin.{n:06d}")
    for n in range(12, 14)  # Process the large early binlogs (000012-000013)
]

def get_column_names():
    """Get patient table column names in order."""
    result = subprocess.run(
        [MYSQL, "-u", "root", "-N", "-e",
         "SELECT COLUMN_NAME FROM information_schema.columns "
         "WHERE table_schema='opendental' AND table_name='patient' "
         "ORDER BY ORDINAL_POSITION"],
        capture_output=True, text=True, timeout=30
    )
    return [line.strip() for line in result.stdout.strip().split('\n') if line.strip()]


def get_existing_patnums():
    """Get all PatNums currently in the patient table."""
    result = subprocess.run(
        [MYSQL, "-u", "root", "-N", "-e",
         "SELECT PatNum FROM opendental.patient"],
        capture_output=True, text=True, timeout=60
    )
    return set(int(line.strip()) for line in result.stdout.strip().split('\n') if line.strip())


def extract_patient_inserts_from_binlog(binlog_file, columns):
    """Parse a binlog file and extract patient table INSERT events."""
    print(f"  Processing {os.path.basename(binlog_file)}...", flush=True)

    try:
        result = subprocess.run(
            [MYSQLBINLOG, "--no-defaults", "--base64-output=decode-rows", "-v",
             "--database=opendental", binlog_file],
            capture_output=True, text=True, timeout=600  # 10 min timeout for large files
        )
    except subprocess.TimeoutExpired:
        print(f"  WARNING: Timeout processing {binlog_file}", flush=True)
        return []

    if result.returncode != 0:
        print(f"  WARNING: mysqlbinlog error: {result.stderr[:500]}", flush=True)
        return []

    # Parse the output for patient table INSERT events
    lines = result.stdout.split('\n')
    inserts = []
    in_patient_insert = False
    current_values = {}
    col_idx = 0

    for i, line in enumerate(lines):
        # Detect start of INSERT INTO patient
        if '### INSERT INTO `opendental`.`patient`' in line:
            in_patient_insert = True
            current_values = {}
            col_idx = 0
            continue

        # Detect end of current event (next ### line that's not SET or a value)
        if in_patient_insert and line.startswith('###'):
            if line.strip().startswith('### SET'):
                continue
            elif line.strip().startswith('### @'):
                # Parse column value: ###   @1=22110 /* ... */
                match = re.match(r'###\s+@(\d+)=(.*?)(?:\s+/\*.*\*/)?$', line.strip())
                if match:
                    idx = int(match.group(1)) - 1  # @1 = column index 0
                    val = match.group(2).strip()
                    if idx < len(columns):
                        current_values[columns[idx]] = val
                continue
            else:
                # End of this INSERT event
                if current_values.get('PatNum'):
                    inserts.append(dict(current_values))
                in_patient_insert = False
                current_values = {}
        elif in_patient_insert and not line.startswith('###'):
            # End of annotated section
            if current_values.get('PatNum'):
                inserts.append(dict(current_values))
            in_patient_insert = False
            current_values = {}

    # Don't forget last event
    if in_patient_insert and current_values.get('PatNum'):
        inserts.append(dict(current_values))

    print(f"  Found {len(inserts)} patient INSERT events", flush=True)
    return inserts


def build_insert_sql(row_data, columns):
    """Build an INSERT ... ON DUPLICATE KEY UPDATE SQL statement."""
    cols = []
    vals = []
    for col in columns:
        if col in row_data:
            cols.append(f"`{col}`")
            val = row_data[col]
            if val == 'NULL':
                vals.append('NULL')
            else:
                # Value is already SQL-formatted from mysqlbinlog output
                vals.append(val)

    if not cols:
        return None

    sql = f"INSERT INTO `patient` ({', '.join(cols)}) VALUES ({', '.join(vals)})"
    # Add ON DUPLICATE KEY UPDATE to handle re-runs
    updates = [f"`{c}`=VALUES(`{c}`)" for c in columns if c in row_data and c != 'PatNum']
    if updates:
        sql += f" ON DUPLICATE KEY UPDATE {', '.join(updates)}"

    return sql


def main():
    print("=== Patient Table Recovery from Binary Logs ===", flush=True)

    # Get column names
    columns = get_column_names()
    print(f"Patient table has {len(columns)} columns", flush=True)

    # Get existing PatNums
    existing = get_existing_patnums()
    max_existing = max(existing) if existing else 0
    print(f"Current patient count: {len(existing)}, max PatNum: {max_existing}", flush=True)

    # Process each binlog
    all_inserts = []
    for binlog in BINLOG_FILES:
        if not os.path.exists(binlog):
            print(f"  Skipping {binlog} (not found)", flush=True)
            continue
        inserts = extract_patient_inserts_from_binlog(binlog, columns)
        all_inserts.extend(inserts)

    # Filter for missing patients only (PatNum > max_existing)
    missing_inserts = {}
    for row in all_inserts:
        try:
            patnum = int(row.get('PatNum', '0'))
        except (ValueError, TypeError):
            continue
        if patnum > max_existing:
            # Keep the LATEST insert for each PatNum (in case of updates)
            missing_inserts[patnum] = row

    print(f"\nFound {len(missing_inserts)} missing patients to recover", flush=True)

    if not missing_inserts:
        print("No missing patients found in binary logs.", flush=True)
        return

    # Generate SQL file
    sql_file = r"D:\temp\patient_recovery.sql"
    with open(sql_file, 'w', encoding='utf-8') as f:
        f.write("USE opendental;\n")
        f.write("SET FOREIGN_KEY_CHECKS=0;\n")
        for patnum in sorted(missing_inserts.keys()):
            sql = build_insert_sql(missing_inserts[patnum], columns)
            if sql:
                f.write(sql + ";\n")
        f.write("SET FOREIGN_KEY_CHECKS=1;\n")

    print(f"Recovery SQL written to {sql_file}", flush=True)
    print(f"Patients to recover: {sorted(missing_inserts.keys())[:20]}{'...' if len(missing_inserts) > 20 else ''}", flush=True)

    # Apply the recovery SQL
    print("\nApplying recovery SQL...", flush=True)
    result = subprocess.run(
        [MYSQL, "-u", "root", "opendental"],
        input=open(sql_file, 'r', encoding='utf-8').read(),
        capture_output=True, text=True, timeout=120
    )

    if result.returncode == 0:
        print("Recovery SQL applied successfully!", flush=True)
    else:
        print(f"Errors during recovery: {result.stderr[:2000]}", flush=True)

    # Verify
    new_existing = get_existing_patnums()
    new_max = max(new_existing) if new_existing else 0
    print(f"\nAfter recovery: {len(new_existing)} patients, max PatNum: {new_max}", flush=True)
    recovered = len(new_existing) - len(existing)
    print(f"Recovered {recovered} patients", flush=True)


if __name__ == "__main__":
    main()
