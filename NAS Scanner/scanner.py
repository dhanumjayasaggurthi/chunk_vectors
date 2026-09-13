# db.py
import psycopg2
from configparser import ConfigParser


def _connect():
    cfg = ConfigParser()
    cfg.read("config.ini")
    c = cfg["POSTGRES"]
    return psycopg2.connect(
        host=c.get("host"),
        port=c.get("port"),
        dbname=c.get("database"),
        user=c.get("user"),
        password=c.get("password")
    )


def is_folder_scanned(folder_path):
    conn = _connect()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM epod1.folder_progress WHERE folder_path = %s", (folder_path,))
    exists = cur.fetchone() is not None
    conn.close()
    return exists


def save_file_data_batch(records):
    if not records:
        return
    conn = _connect()
    cur = conn.cursor()
    cur.executemany("""
                    INSERT INTO epod1.file_data (folder_path, file_name, file_size_mb, last_modified)
                    VALUES (%s, %s, %s, %s) ON CONFLICT (folder_path, file_name) DO NOTHING;
                    """, records)
    conn.commit()
    conn.close()


def save_folder_summary(data):
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
                INSERT INTO epod1.folder_progress
                (folder_path, total_files, pdf_files, xml_files, other_files,
                 total_size_mb, pdf_size_mb, xml_size_mb, other_size_mb, last_modified)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (folder_path) DO
                UPDATE
                    SET total_files = EXCLUDED.total_files,
                    pdf_files = EXCLUDED.pdf_files,
                    xml_files = EXCLUDED.xml_files,
                    other_files = EXCLUDED.other_files,
                    total_size_mb = EXCLUDED.total_size_mb,
                    pdf_size_mb = EXCLUDED.pdf_size_mb,
                    xml_size_mb = EXCLUDED.xml_size_mb,
                    other_size_mb = EXCLUDED.other_size_mb,
                    last_modified = EXCLUDED.last_modified;
                """, data)
    conn.commit()
    conn.close()


# scanner_worker.py
import os
from datetime import datetime


# from db import save_file_data_batch, save_folder_summary

def scan_one_folder(folder_path):
    total = pdf = xml = other = 0
    pdf_sz = xml_sz = other_sz = 0
    batch = []
    last_mod = None

    print(f"▶️  START: {folder_path}", flush=True)

    start_time = time()
    file_count = 0
    last_heartbeat = start_time

    for root, dirs, files in os.walk(folder_path):
        for f in files:
            file_count += 1
            total += 1
            fp = os.path.join(root, f)

            try:
                size = os.path.getsize(fp)
                last_mod = datetime.fromtimestamp(os.path.getmtime(fp)).strftime('%Y-%m-%d %H:%M:%S')

                fn = f.lower()
                if fn.endswith(".pdf"):
                    pdf += 1;
                    pdf_sz += size
                elif fn.endswith(".xml"):
                    xml += 1;
                    xml_sz += size
                else:
                    other += 1;
                    other_sz += size

                batch.append((folder_path, f, size, last_mod))

                # ---- BATCH SAVE ----
                if len(batch) >= 2000:
                    save_file_data_batch(batch)
                    batch = []

            except:
                continue

            # ---- HEARTBEAT EVERY 5 SECONDS ----
            now = time()
            if now - last_heartbeat >= 5:
                elapsed = now - start_time
                print(f"   🔄 {folder_path} → {file_count:,} files scanned (elapsed {elapsed:.1f}s)", flush=True)
                last_heartbeat = now

    # Save remaining batch
    if batch:
        save_file_data_batch(batch)

    # Save summary
    save_folder_summary((
        folder_path, total, pdf, xml, other,
        pdf_sz + xml_sz + other_sz,
        pdf_sz, xml_sz, other_sz,
        last_mod
    ))

    total_time = time() - start_time
    print(f"✅ DONE: {folder_path} ({total:,} files, {total_time / 60:.1f} min)", flush=True)

    return folder_path, total


# scanner.py

import os
# from db import is_folder_scanned
# from scanner_worker import scan_one_folder
from concurrent.futures import ProcessPoolExecutor, as_completed
from tqdm import tqdm
from time import time


def discover_folders(base_directory):
    folders = []
    count = 0
    last_print = time()

    for root, dirs, files in os.walk(base_directory):
        count += 1

        # Only queue folders we haven't scanned
        if not is_folder_scanned(root):
            folders.append(root)

        # Heartbeat every 3 seconds
        if time() - last_print >= 3:
            print(f"   📁 Discovered {count:,} folders so far... {len(folders):,} pending", flush=True)
            last_print = time()

    return folders


def scan(base_directory, workers=4):
    print("🔍 Scanning directory tree...")
    folders = discover_folders(base_directory)
    print(f"📂 Remaining folders to scan: {len(folders):,}\n")

    with ProcessPoolExecutor(max_workers=workers) as exe:
        futures = {exe.submit(scan_one_folder, f): f for f in folders}

        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                print(f"⚠️  ERROR: {e}", flush=True)


if __name__ == "__main__":
    from threading import Thread

    cfg = ConfigParser()
    cfg.read("config.ini")
    s = cfg["SCANNER"]

    paths = [p.strip() for p in s.get("paths").split(",") if p.strip()]
    workers = [int(w.strip()) for w in s.get("workers").split(",")]

    if len(workers) == 1:
        workers = workers * len(paths)

    if len(paths) != len(workers):
        raise ValueError(f"Mismatch: {len(paths)} paths but {len(workers)} worker counts in config.ini")

    threads = []
    for path, w in zip(paths, workers):
        print(f"\n🚀 Launching scan: {path} with {w} workers")
        t = Thread(target=scan, args=(path, w))
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    print("\n✅ All paths completed!")