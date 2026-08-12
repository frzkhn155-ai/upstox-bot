from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==== CONFIGURATION ====
NUM_WORKERS = 1  # start with 1; increase later

input_file = r'C:\Users\cb99354\Desktop\1.txt'
notfound_file = r'C:\Users\cb99354\Desktop\notfound.txt'
status_file = r'C:\Users\cb99354\Desktop\status.txt'

# Clear status at start
open(status_file, 'w').close()

# Team names (unchanged)
TEAM_NAMES = [
    "INFANT", "DINESH", "DURGA", "ELUMALAI", "FEROZ", "PRIYA",
    "SATISH", "SNEHA", "SOWMIYA", "SRINATH", "THIRU", "LOKESH", "PREETI",
]

def levenshtein(a, b):
    if len(a) < len(b):
        return levenshtein(b, a)
    if len(b) == 0:
        return len(a)
    previous_row = range(len(b) + 1)
    for i, ca in enumerate(a):
        current_row = [i + 1]
        for j, cb in enumerate(b):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (ca != cb)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    return previous_row[-1]

def is_team_member(updated_by_text, max_distance=2):
    if not updated_by_text:
        return False, None
    name_part = updated_by_text.split('(')[0].strip().upper()
    words = [w for w in name_part.split() if w]
    for word in words:
        for team_name in TEAM_NAMES:
            if levenshtein(word, team_name) <= max_distance:
                return True, team_name
    return False, None

# Shared structures
results = {}
updater_names = {}
lock = threading.Lock()
file_lock = threading.Lock()

def write_status(loan_no, status, updater_name):
    with file_lock:
        with open(status_file, 'a', encoding='utf-8') as f:
            f.write(f"{loan_no}\t{status}\t{updater_name}\n")

# Read records (same as original)
records = []
with open(input_file, 'r', encoding='utf-8') as f:
    for idx, line in enumerate(f):
        parts = line.strip().split('\t')
        if len(parts) >= 9:
            date_txt = parts[0]
            pod_no = parts[4]
            document_type = parts[5].strip()
            orig_status = parts[6].strip()
            loan_no = parts[-1]
            remark_val = f"{document_type} {orig_status}"
            records.append({
                'index': idx,
                'date_txt': date_txt,
                'pod_no': pod_no,
                'remark': remark_val,
                'loan_no': loan_no,
                'document_type': document_type
            })

print(f"Total records: {len(records)}, using {NUM_WORKERS} worker(s).")

# POD matching (same as original)
def pod_matches(input_pod, dms_pod, min_match_length=8):
    input_pod = input_pod.strip()
    dms_pod = dms_pod.strip()
    if input_pod == dms_pod:
        return True
    if input_pod in dms_pod or dms_pod in input_pod:
        return True
    longer = input_pod if len(input_pod) >= len(dms_pod) else dms_pod
    shorter = dms_pod if len(input_pod) >= len(dms_pod) else input_pod
    if len(shorter) >= min_match_length:
        for i in range(len(shorter) - min_match_length + 1):
            if shorter[i:i+min_match_length] in longer:
                return True
    return False

# =====================================================
# PROCESS ONE LOAN (original logic, only update link fix)
# =====================================================
def process_loan(rec, worker_id, driver):
    idx = rec['index']
    loan_no = rec['loan_no']
    pod_no = rec['pod_no']
    date_txt = rec['date_txt']
    remark_val = rec['remark']
    document_type = rec['document_type']
    
    status_result = "NO"
    updater_name = ""
    
    try:
        loan_url = f"https://dcm.chola.murugappa.com/loan-details/{loan_no}"
        driver.get(loan_url)
        time.sleep(2)
        
        # Click Dispatch tab
        try:
            dispatch_tab = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.ID, "pills-profile-tab"))
            )
            dispatch_tab.click()
            time.sleep(2)
            # Wait for table rows
            WebDriverWait(driver, 10).until(
                EC.presence_of_all_elements_located((By.XPATH, "//table/tbody/tr"))
            )
        except Exception as ex:
            print(f"[W{worker_id}] [{loan_no}] Dispatch tab error: {ex}")
            status_result = "NA" if document_type.upper() == "OTC" else "NO"
            write_status(loan_no, status_result, updater_name)
            with lock:
                results[idx] = status_result
                updater_names[idx] = updater_name
            return
        
        found = False
        table_rows = driver.find_elements(By.XPATH, "//table/tbody/tr")
        for row in table_rows:
            cells = row.find_elements(By.TAG_NAME, "td")
            if cells:
                dms_pod = cells[0].text.strip()
                if pod_matches(pod_no, dms_pod):
                    found = True
                    print(f"[W{worker_id}] [{loan_no}] POD matched: Input={pod_no}, DMS={dms_pod}")
                    
                    # parse date
                    if '.' in date_txt:
                        day, month, year = date_txt.split('.')
                    elif '-' in date_txt:
                        day, month, year = date_txt.split('-')
                    else:
                        raise ValueError(f"Bad date: {date_txt}")
                    date_formatted = f"{day}/{month}/{year}"
                    
                    # ---------- FIX: robust update element ----------
                    try:
                        # Use flexible XPath to find any Update link/button
                        update_element = WebDriverWait(row, 5).until(
                            EC.presence_of_element_located(
                                (By.XPATH, ".//a[contains(text(),'Update')] | .//button[contains(text(),'Update')] | .//input[@value='Update']")
                            )
                        )
                        driver.execute_script("arguments[0].scrollIntoView(true);", update_element)
                        update_element.click()
                        time.sleep(2)
                        
                        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, "status")))
                        Select(driver.find_element(By.ID, "status")).select_by_visible_text("Received")
                        date_input = driver.find_element(By.ID, "received_date")
                        date_input.clear()
                        date_input.send_keys(date_formatted)
                        time.sleep(1)
                        remark_input = driver.find_element(By.ID, "remark")
                        remark_input.clear()
                        remark_input.send_keys(remark_val)
                        time.sleep(1)
                        driver.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
                        print(f"[W{worker_id}] [{loan_no}] POD {dms_pod} updated.")
                        status_result = "YES"
                        updater_name = "cb112700 (script)"
                    except Exception as e:
                        # Update not found – check if already updated by team member
                        try:
                            if len(cells) > 7:
                                updated_by_text = cells[6].text.strip()
                                received_at_text = cells[7].text.strip()
                                received_at_filled = bool(received_at_text) and received_at_text != "-"
                                date_ok = received_at_filled and received_at_text == date_formatted
                                member_ok, matched = is_team_member(updated_by_text)
                                if date_ok and member_ok:
                                    status_result = "YES"
                                    updater_name = updated_by_text
                                    print(f"[W{worker_id}] [{loan_no}] Already updated by {matched}")
                                else:
                                    status_result = "NO"
                                    print(f"[W{worker_id}] [{loan_no}] Date mismatch or not team.")
                            else:
                                status_result = "NO"
                        except:
                            status_result = "NO"
                    
                    write_status(loan_no, status_result, updater_name)
                    break
        
        if not found:
            status_result = "NA" if document_type.upper() == "OTC" else "NO"
            print(f"[W{worker_id}] [{loan_no}] POD not found.")
            write_status(loan_no, status_result, updater_name)
    except Exception as e:
        print(f"[W{worker_id}] [{loan_no}] Error: {e}")
        status_result = "NA" if document_type.upper() == "OTC" else "NO"
        write_status(loan_no, status_result, updater_name)
    
    with lock:
        results[idx] = status_result
        updater_names[idx] = updater_name

# =====================================================
# WORKER – uses original driver creation and login
# =====================================================
def worker_function(records_batch, worker_id):
    if not records_batch:
        return
    print(f"[W{worker_id}] Starting, {len(records_batch)} records.")
    
    # ---- Original driver creation ----
    driver = webdriver.Chrome()  # No service/options – as in your working script
    
    try:
        # ---- Login with simple retry ----
        max_attempts = 3
        for attempt in range(1, max_attempts+1):
            try:
                driver.get("https://dcm.chola.murugappa.com/login")
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.ID, "username"))
                ).send_keys("cb112700")
                driver.find_element(By.ID, "password").send_keys("Naruto@@2005")
                driver.find_element(By.CLASS_NAME, "btn-primary").click()
                time.sleep(3)  # Wait for login to complete – same as original
                print(f"[W{worker_id}] Login successful (attempt {attempt}).")
                break
            except Exception as e:
                print(f"[W{worker_id}] Login attempt {attempt} failed: {e}")
                if attempt == max_attempts:
                    raise
                time.sleep(2 ** attempt)
        
        # ---- Process records ----
        for idx, rec in enumerate(records_batch, 1):
            print(f"[W{worker_id}] {idx}/{len(records_batch)} -> {rec['loan_no']}")
            process_loan(rec, worker_id, driver)
            time.sleep(1)
        print(f"[W{worker_id}] Completed.")
    except Exception as e:
        print(f"[W{worker_id}] Fatal error: {e}")
        # Mark remaining as ERROR
        for rec in records_batch:
            write_status(rec['loan_no'], "ERROR", "Worker failed")
            with lock:
                results[rec['index']] = "ERROR"
                updater_names[rec['index']] = "Login failed"
    finally:
        driver.quit()

# Split and run (same as original)
def split_records(all_records, num_workers):
    if num_workers <= 0:
        num_workers = 1
    if num_workers > len(all_records):
        num_workers = len(all_records)
    base = len(all_records) // num_workers
    rem = len(all_records) % num_workers
    batches = []
    start = 0
    for i in range(num_workers):
        end = start + base + (1 if i < rem else 0)
        batches.append(all_records[start:end])
        start = end
    return batches

if __name__ == "__main__":
    start_time = time.time()
    batches = split_records(records, NUM_WORKERS)
    with ThreadPoolExecutor(max_workers=len(batches)) as executor:
        futures = [executor.submit(worker_function, batch, i+1) for i, batch in enumerate(batches) if batch]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as e:
                print(f"Worker error: {e}")

    # Write notfound.txt
    with open(notfound_file, 'w', encoding='utf-8') as nf:
        for idx, status in sorted(results.items()):
            if status == "NO":
                rec = next((r for r in records if r['index'] == idx), None)
                if rec:
                    nf.write(f"{rec['loan_no']}\n")
            elif status == "NA":
                rec = next((r for r in records if r['index'] == idx), None)
                if rec:
                    nf.write(f"{rec['loan_no']}\tOTC-NA\n")

    elapsed = time.time() - start_time
    yes = sum(1 for s in results.values() if s == "YES")
    no = sum(1 for s in results.values() if s == "NO")
    na = sum(1 for s in results.values() if s == "NA")
    err = sum(1 for s in results.values() if s == "ERROR")
    print("\n" + "="*70)
    print("SCRIPT COMPLETED")
    print(f"Total: {len(records)} | YES: {yes} | NO: {no} | NA: {na} | ERROR: {err}")
    print(f"Workers: {len(batches)} | Time: {elapsed:.2f}s")
    print("Check status.txt (live) and notfound.txt")
    print("="*70)