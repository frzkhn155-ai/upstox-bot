from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import random
import sys

# ---- Use webdriver_manager to auto-handle ChromeDriver ----
try:
    from webdriver_manager.chrome import ChromeDriverManager
except ImportError:
    print("ERROR: webdriver-manager not installed.")
    print("Please run: pip install webdriver-manager")
    sys.exit(1)

# ==== CONFIGURATION ====
NUM_WORKERS = 1
MAX_LOGIN_ATTEMPTS = 3
RETRY_DELAY_BASE = 2

DESKTOP = os.path.expanduser("~/Desktop")
DEFAULT_INPUT = os.path.join(DESKTOP, "1.txt")
NOTFOUND_FILE = os.path.join(DESKTOP, "notfound.txt")
STATUS_FILE = os.path.join(DESKTOP, "status.txt")
TEMP_STATUS_FILE = os.path.join(DESKTOP, "status_temp.txt")

if len(sys.argv) > 1:
    input_file = sys.argv[1]
else:
    input_file = DEFAULT_INPUT

# Team names (unchanged)
TEAM_NAMES = [
    "INFANT", "DINESH", "DURGA", "ELUMALAI", "FEROZ", "PRIYA",
    "SATISH", "SNEHA", "SOWMIYA", "SRINATH", "THIRU", "LOKESH", "PREETI",
]

# ==== UTILITY FUNCTIONS (unchanged) ====
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
            substring = shorter[i:i + min_match_length]
            if substring in longer:
                return True
    return False

# ==== IMMEDIATE WRITE ====
write_lock = threading.Lock()

def write_result(loan_no, status, updater_name=""):
    with write_lock:
        with open(TEMP_STATUS_FILE, 'a', encoding='utf-8') as f:
            f.write(f"{loan_no}\t{status}\t{updater_name}\n")

# ==== CORE PROCESSING (improved) ====
def process_loan(rec, worker_id, driver):
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

        try:
            dispatch_tab = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.ID, "pills-profile-tab"))
            )
            dispatch_tab.click()
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.XPATH, "//table/tbody/tr"))
            )
        except Exception as ex:
            print(f"[Worker-{worker_id}] [{loan_no}] Dispatch tab/table issue: {ex}")
            if document_type.upper() == "OTC":
                status_result = "NA"
            else:
                status_result = "NO"
            write_result(loan_no, status_result, updater_name)
            return

        found = False
        table_rows = driver.find_elements(By.XPATH, "//table/tbody/tr")

        for row in table_rows:
            cells = row.find_elements(By.TAG_NAME, "td")
            if not cells:
                continue
            dms_pod = cells[0].text.strip()

            if pod_matches(pod_no, dms_pod):
                found = True
                print(f"[Worker-{worker_id}] [{loan_no}] POD matched: Input={pod_no}, DMS={dms_pod}")

                if '.' in date_txt:
                    day, month, year = date_txt.split('.')
                elif '-' in date_txt:
                    day, month, year = date_txt.split('-')
                else:
                    raise ValueError(f"Unrecognized date format: {date_txt}")
                date_formatted = f"{day}/{month}/{year}"

                update_found = False
                try:
                    update_element = None
                    try:
                        update_element = row.find_element(By.LINK_TEXT, "Update")
                    except NoSuchElementException:
                        pass
                    if not update_element:
                        update_element = row.find_element(By.XPATH, ".//*[contains(translate(text(), 'UPDATE', 'update'), 'update')]")

                    if update_element:
                        driver.execute_script("arguments[0].scrollIntoView(true);", update_element)
                        try:
                            update_element.click()
                        except Exception:
                            driver.execute_script("arguments[0].click();", update_element)

                        WebDriverWait(driver, 10).until(
                            EC.presence_of_element_located((By.ID, "status"))
                        )

                        Select(driver.find_element(By.ID, "status")).select_by_visible_text("Received")
                        date_input = driver.find_element(By.ID, "received_date")
                        date_input.clear()
                        date_input.send_keys(date_formatted)
                        time.sleep(1)

                        remark_input = driver.find_element(By.ID, "remark")
                        remark_input.clear()
                        remark_input.send_keys(remark_val)
                        time.sleep(1)

                        submit_btn = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
                        submit_btn.click()

                        print(f"[Worker-{worker_id}] [{loan_no}] POD {dms_pod} updated")
                        status_result = "YES"
                        updater_name = "cb112700 (script)"
                        update_found = True
                        write_result(loan_no, status_result, updater_name)
                        break

                except Exception as e:
                    print(f"[Worker-{worker_id}] [{loan_no}] Update action failed: {e}")

                if not update_found:
                    try:
                        if len(cells) > 7:
                            updated_by_text = cells[6].text.strip()
                            received_at_text = cells[7].text.strip()
                            received_at_filled = bool(received_at_text) and received_at_text != "-"
                            date_ok = received_at_filled and received_at_text == date_formatted
                            member_ok, matched_team_name = is_team_member(updated_by_text)

                            if date_ok and member_ok:
                                print(f"[Worker-{worker_id}] [{loan_no}] Date matches & team member - YES")
                                status_result = "YES"
                                updater_name = updated_by_text if updated_by_text else "(blank)"
                            elif date_ok and not member_ok:
                                print(f"[Worker-{worker_id}] [{loan_no}] Date matches but not team member - NO")
                                status_result = "NO"
                            else:
                                print(f"[Worker-{worker_id}] [{loan_no}] Date mismatch or not filled - NO")
                                status_result = "NO"
                            write_result(loan_no, status_result, updater_name)
                        else:
                            print(f"[Worker-{worker_id}] [{loan_no}] Cannot verify Received At - NO")
                            status_result = "NO"
                            write_result(loan_no, status_result, updater_name)
                    except Exception as check_ex:
                        print(f"[Worker-{worker_id}] [{loan_no}] Error checking existing data: {check_ex}")
                        status_result = "NO"
                        write_result(loan_no, status_result, updater_name)

                break

        if not found:
            if document_type.upper() == "OTC":
                print(f"[Worker-{worker_id}] [{loan_no}] POD not found - OTC NA")
                status_result = "NA"
            else:
                print(f"[Worker-{worker_id}] [{loan_no}] POD not found")
                status_result = "NO"
            write_result(loan_no, status_result, updater_name)

    except Exception as e:
        print(f"[Worker-{worker_id}] Error processing loan {loan_no}: {e}")
        if document_type.upper() == "OTC":
            status_result = "NA"
        else:
            status_result = "NO"
        write_result(loan_no, status_result, updater_name)

# ==== WORKER WITH LOGIN RETRY AND AUTO-DRIVER ====
def worker_function(records_batch, worker_id):
    if not records_batch:
        return

    print(f"[Worker-{worker_id}] Starting, {len(records_batch)} records")

    driver = None
    for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
        try:
            if driver is not None:
                driver.quit()

            # ---- Use ChromeDriverManager to get the correct driver ----
            service = Service(ChromeDriverManager().install())
            options = webdriver.ChromeOptions()
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            # Uncomment next line if you want headless (no GUI)
            # options.add_argument("--headless")

            driver = webdriver.Chrome(service=service, options=options)

            driver.get("https://dcm.chola.murugappa.com/login")
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.ID, "username"))
            ).send_keys("cb112700")

            driver.find_element(By.ID, "password").send_keys("Naruto@@2005")
            WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.CLASS_NAME, "btn-primary"))
            ).click()

            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.XPATH, "//*[contains(text(), 'Dashboard')]"))
            )
            print(f"[Worker-{worker_id}] Login successful on attempt {attempt}")
            break
        except Exception as e:
            print(f"[Worker-{worker_id}] Login attempt {attempt} failed: {e}")
            if attempt == MAX_LOGIN_ATTEMPTS:
                print(f"[Worker-{worker_id}] All login attempts failed, aborting.")
                if driver:
                    driver.quit()
                return
            delay = RETRY_DELAY_BASE ** attempt + random.uniform(0, 1)
            time.sleep(delay)

    for idx, rec in enumerate(records_batch, 1):
        print(f"[Worker-{worker_id}] {idx}/{len(records_batch)} -> {rec['loan_no']}")
        process_loan(rec, worker_id, driver)
        time.sleep(1)

    driver.quit()
    print(f"[Worker-{worker_id}] Completed.")

# ==== MAIN ====
if __name__ == "__main__":
    start_time = time.time()

    try:
        with open(input_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except FileNotFoundError:
        print(f"ERROR: Input file not found: {input_file}")
        print(f"Please provide correct path, e.g.: python {os.path.basename(__file__)} C:\\path\\to\\file.txt")
        sys.exit(1)

    records = []
    for idx, line in enumerate(lines):
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

    print(f"Total records to process: {len(records)}")
    print(f"Using {NUM_WORKERS} parallel workers")
    print(f"Input file: {input_file}")

    open(TEMP_STATUS_FILE, 'w').close()

    def split_records(all_records, num_workers):
        if num_workers <= 0:
            num_workers = 1
        if num_workers > len(all_records):
            num_workers = len(all_records)
        base = len(all_records) // num_workers
        rem = len(all_records) % num_workers
        batches, start = [], 0
        for i in range(num_workers):
            end = start + base + (1 if i < rem else 0)
            batches.append(all_records[start:end])
            start = end
        return batches

    batches = split_records(records, NUM_WORKERS)

    with ThreadPoolExecutor(max_workers=len(batches)) as executor:
        futures = [executor.submit(worker_function, batch, wid)
                   for wid, batch in enumerate(batches, 1) if batch]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as e:
                print(f"Worker error: {e}")

    # Merge temp results
    results_by_loan = {}
    with open(TEMP_STATUS_FILE, 'r', encoding='utf-8') as tf:
        for line in tf:
            parts = line.strip().split('\t')
            if len(parts) >= 2:
                loan = parts[0]
                status = parts[1]
                name = parts[2] if len(parts) > 2 else ""
                results_by_loan[loan] = (status, name)

    with open(STATUS_FILE, 'w', encoding='utf-8') as sf:
        for rec in records:
            loan = rec['loan_no']
            status, name = results_by_loan.get(loan, ("ERROR", ""))
            sf.write(f"{loan}\t{status}\t{name}\n")

    with open(NOTFOUND_FILE, 'w', encoding='utf-8') as nf:
        for rec in records:
            loan = rec['loan_no']
            status, _ = results_by_loan.get(loan, ("ERROR", ""))
            if status == "NO":
                nf.write(f"{loan}\n")
            elif status == "NA":
                nf.write(f"{loan}\tOTC-NA\n")

    yes = sum(1 for s, _ in results_by_loan.values() if s == "YES")
    no = sum(1 for s, _ in results_by_loan.values() if s == "NO")
    na = sum(1 for s, _ in results_by_loan.values() if s == "NA")
    elapsed = time.time() - start_time

    print("\n" + "=" * 70)
    print("SCRIPT COMPLETED")
    print("=" * 70)
    print(f"Total records       : {len(records)}")
    print(f"Successfully updated: {yes}")
    print(f"Not found / failed  : {no}")
    print(f"OTC - NA            : {na}")
    print(f"Workers used        : {len(batches)}")
    print(f"Time taken (sec)    : {elapsed:.2f}")
    print(f"Status file: {STATUS_FILE}")
    print(f"Not found file: {NOTFOUND_FILE}")
    print("=" * 70)