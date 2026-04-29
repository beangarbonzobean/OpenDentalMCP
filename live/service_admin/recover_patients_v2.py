"""
Recover patient table rows from MariaDB binary logs (v2).
Streams through mysqlbinlog output and captures patient table INSERT events.
Processes binlogs one at a time to manage memory and time.
"""
import subprocess
import sys
import re
import os

MYSQLBINLOG = r"C:\Program Files\MariaDB 10.11\bin\mysqlbinlog.exe"
MYSQL = r"C:\Program Files\MariaDB 10.11\bin\mysql.exe"
DATADIR = r"D:\mysql\data"
SQL_OUT = r"D:\temp\patient_recovery.sql"

def get_column_names():
    result = subprocess.run(
        [MYSQL, "-u", "root", "-N", "-e",
         "SELECT COLUMN_NAME FROM information_schema.columns "
         "WHERE table_schema='opendental' AND table_name='patient' "
         "ORDER BY ORDINAL_POSITION"],
        capture_output=True, text=True, timeout=30
    )
    return [line.strip() for line in result.stdout.strip().split('\n') if line.strip()]

def get_max_patnum():
    result = subprocess.run(
        [MYSQL, "-u", "root", "-N", "-e",
         "SELECT MAX(PatNum) FROM opendental.patient"],
        capture_output=True, text=True, timeout=30
    )
    return int(result.stdout.strip())

def get_patient_count():
    result = subprocess.run(
        [MYSQL, "-u", "root", "-N", "-e",
         "SELECT COUNT(*) FROM opendental.patient"],
        capture_output=True, text=True, timeout=30
    )
    return int(result.stdout.strip())

def process_binlog(binlog_file, columns, max_patnum):
    """Stream through a binlog and extract patient INSERT events."""
    fname = os.path.basename(binlog_file)
    print(f"  Streaming {fname} ({os.path.getsize(binlog_file)/(1024*1024):.0f} MB)...", flush=True)

    # Use mysqlbinlog to decode, stream line by line
    proc = subprocess.Popen(
        [MYSQLBINLOG, "--no-defaults", "--base64-output=decode-rows", "-v",
         "--database=opendental", binlog_file],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, encoding='utf-8', errors='replace'
    )

    inserts = []
    in_patient_event = False
    event_type = None  # 'insert' or 'update'
    current_values = {}
    update_after = False  # For UPDATE, track SET (before) vs SET (after)

    for line in proc.stdout:
        line = line.rstrip('\n')

        # Detect patient table events
        if '### INSERT INTO `opendental`.`patient`' in line:
            in_patient_event = True
            event_type = 'insert'
            current_values = {}
            continue
        elif '### UPDATE `opendental`.`patient`' in line:
            in_patient_event = True
            event_type = 'update'
            current_values = {}
            update_after = False
            continue

        if in_patient_event:
            stripped = line.strip()
            if stripped == '### SET':
                if event_type == 'update' and current_values:
                    # This is the "after" SET in an UPDATE (WHERE was the before)
                    update_after = True
                    current_values = {}
                continue
            elif stripped == '### WHERE':
                # For UPDATE: WHERE contains the "before" image, skip it
                # Reset to capture the SET (after) values
                current_values = {}
                update_after = False
                continue
            elif stripped.startswith('### @'):
                # Parse: ###   @1=22110 /* LONGINT meta=0 nullable=0 is_null=0 */
                match = re.match(r'###\s+@(\d+)=(.*?)(?:\s+/\*.*\*/)?$', stripped)
                if match:
                    idx = int(match.group(1)) - 1
                    val = match.group(2).strip()
                    if idx < len(columns):
                        current_values[columns[idx]] = val
                continue
            elif stripped.startswith('###'):
                # Another ### event (e.g., next INSERT/UPDATE/DELETE)
                # Save current event
                if current_values.get('PatNum'):
                    try:
                        pn = int(current_values['PatNum'])
                        if pn > max_patnum:
                            inserts.append(dict(current_values))
                    except (ValueError, TypeError):
                        pass
                in_patient_event = False
                current_values = {}

                # Check if this line starts a new patient event
                if '### INSERT INTO `opendental`.`patient`' in stripped:
                    in_patient_event = True
                    event_type = 'insert'
                    current_values = {}
                elif '### UPDATE `opendental`.`patient`' in stripped:
                    in_patient_event = True
                    event_type = 'update'
                    current_values = {}
                    update_after = False
                continue
            else:
                # Non-### line means end of annotated section
                if current_values.get('PatNum'):
                    try:
                        pn = int(current_values['PatNum'])
                        if pn > max_patnum:
                            inserts.append(dict(current_values))
                    except (ValueError, TypeError):
                        pass
                in_patient_event = False
                current_values = {}

    # Final event
    if in_patient_event and current_values.get('PatNum'):
        try:
            pn = int(current_values['PatNum'])
            if pn > max_patnum:
                inserts.append(dict(current_values))
        except (ValueError, TypeError):
            pass

    proc.wait()
    print(f"  Found {len(inserts)} new patient events in {fname}", flush=True)
    return inserts

def build_insert_sql(row_data, columns):
    cols = []
    vals = []
    for col in columns:
        if col in row_data:
            cols.append(f"`{col}`")
            val = row_data[col]
            if val == 'NULL':
                vals.append('NULL')
            else:
                vals.append(val)
    if not cols:
        return None
    sql = f"INSERT INTO `patient` ({', '.join(cols)}) VALUES ({', '.join(vals)})"
    updates = [f"`{c}`=VALUES(`{c}`)" for c in columns if c in row_data and c != 'PatNum']
    if updates:
        sql += f" ON DUPLICATE KEY UPDATE {', '.join(updates)}"
    return sql

def main():
    print("=== Patient Table Recovery v2 ===", flush=True)

    columns = get_column_names()
    print(f"Patient table: {len(columns)} columns", flush=True)

    max_patnum = get_max_patnum()
    count_before = get_patient_count()
    print(f"Current: {count_before} patients, max PatNum: {max_patnum}", flush=True)

    # Process binlogs in order
    binlog_files = [
        os.path.join(DATADIR, f"mysql-bin.{n:06d}")
        for n in range(12, 19)
    ]

    # Deduplicate: keep last insert per PatNum
    all_rows = {}
    for bf in binlog_files:
        if not os.path.exists(bf):
            continue
        rows = process_binlog(bf, columns, max_patnum)
        for r in rows:
            try:
                pn = int(r['PatNum'])
                all_rows[pn] = r  # Latest wins
            except (ValueError, TypeError):
                pass

    print(f"\nTotal unique new patients found: {len(all_rows)}", flush=True)

    if not all_rows:
        print("No new patients found in binary logs.", flush=True)
        return

    # Write SQL
    with open(SQL_OUT, 'w', encoding='utf-8') as f:
        f.write("USE opendental;\nSET FOREIGN_KEY_CHECKS=0;\n")
        for pn in sorted(all_rows.keys()):
            sql = build_insert_sql(all_rows[pn], columns)
            if sql:
                f.write(sql + ";\n")
        f.write("SET FOREIGN_KEY_CHECKS=1;\n")

    print(f"SQL written to {SQL_OUT}", flush=True)
    first_5 = sorted(all_rows.keys())[:5]
    last_5 = sorted(all_rows.keys())[-5:]
    print(f"PatNum range: {min(all_rows.keys())} - {max(all_rows.keys())}", flush=True)
    print(f"First 5: {first_5}", flush=True)
    print(f"Last 5: {last_5}", flush=True)

    # Apply
    print("\nApplying recovery SQL...", flush=True)
    with open(SQL_OUT, 'r', encoding='utf-8') as f:
        sql_content = f.read()
    result = subprocess.run(
        [MYSQL, "-u", "root", "opendental"],
        input=sql_content,
        capture_output=True, text=True, timeout=120
    )
    if result.returncode == 0:
        print("SUCCESS!", flush=True)
    else:
        print(f"Errors: {result.stderr[:2000]}", flush=True)

    count_after = get_patient_count()
    print(f"Patients before: {count_before}, after: {count_after}, recovered: {count_after - count_before}", flush=True)

if __name__ == "__main__":
    main()
