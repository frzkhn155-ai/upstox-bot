from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==== CONFIGURATION ====
NUM_WORKERS = 1  # minimum 2; increase if your PC and site can handle more

input_file = r'C:\Users\cb99354\Desktop\1.txt'
notfound_file = r'C:\Users\cb99354\Desktop\notfound.txt'
status_file = r'C:\Users\cb99354\Desktop\status.txt'

# Known team member first names, used as an additional confirmation check
# alongside the Received At date match. Matching tolerates 1-2 letter typos
# and short prefixes (e.g., RAG, RAGU -> RAGUPATHI).
TEAM_NAMES = [
    "INFANT", "DINESH", "DURGA", "ELUMALAI", "FEROZ", "PRIYA",
    "SATISH", "SNEHA", "SOWMIYA", "SRINATH", "THIRU", "LOKESH", "PREETI",
    "RAGUPATHI", "RAGU", "RAG",  # added short/full forms
]


def levenshtein(a, b):
    """Edit distance between two strings (for fuzzy name matching)."""
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
    """
    Fuzzy-match the 'Updated By' name against TEAM_NAMES.
    Supports:
      - 1-2 letter spelling differences (e.g., 'Feroz Khan (CB99354)' -> FEROZ)
      - Short prefixes (e.g., 'RAG', 'RAGU' -> RAGUPATHI)
    Returns (True, matched_team_name) or (False, None).
    """
    if not updated_by_text:
        return False, None
    name_part = updated_by_text.split('(')[0].strip().upper()
    words = [w for w in name_part.split() if w]
    for word in words:
        for team_name in TEAM_NAMES:
            if levenshtein(word, team_name) <= max_distance:
                return True, team_name
            if len(word) >= 3:
                if team_name.startswith(word) or word.startswith(team_name):
                    return True, team_name
    return False, None


# Shared data structures with thread-safe lock
results = {}          # {index: 'YES'/'NO'/'NA'}
updater_names = {}    # {index: 'name who updated / blank'}
lock = threading.Lock()

# ==== READ INPUT RECORDS ====
records = []
original_lines = []
with open(input_file, 'r', encoding='utf-8') as f:
    for idx, line in enumerate(f):
        original_lines.append(line.strip())
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
print(f"Using {NUM_WORKERS} parallel workers\n")

# ==== POD MATCHING FUNCTION ====
def pod_matches(input_pod, dms_pod, min_match_length=8):
    """
    Flexible POD matching that handles extra/missing digits anywhere.
    Returns True if there's significant overlap between the two POD numbers.
    """
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


# ==== ROBUST "UPDATE" ELEMENT LOCATOR ====
def find_update_element(driver, row, timeout=5):
    """
    Robustly find the Update/Edit action inside a table row.
    Tries JavaScript first (handles nested icons, title attributes, etc.),
    then falls back to Selenium locators. Returns WebElement or None.
    """
    strategies = [
        (By.LINK_TEXT, "Update"),
        (By.PARTIAL_LINK_TEXT, "Update"),
        (By.XPATH, ".//a[normalize-space()='Update']"),
        (By.XPATH, ".//button[normalize-space()='Update']"),
        (By.XPATH, ".//a[contains(@title, 'Update') or contains(@data-original-title, 'Update')]"),
        (By.XPATH, ".//button[contains(@title, 'Update')]"),
        (By.XPATH, ".//*[contains(@onclick, 'update') or contains(@href, 'update')]"),
        (By.CSS_SELECTOR, "a[title*='Update' i], a[data-original-title*='Update' i], button[title*='Update' i]"),
    ]

    end = time.time() + timeout
    while time.time() < end:
        # 1) JavaScript first — handles nested icons, title="Edit", etc.
        try:
            el = driver.execute_script(
                """
                var row = arguments[0];
                if (!row || !row.querySelectorAll) return null;

                var nodes = row.querySelectorAll('a, button, i, span');
                for (var i = 0; i < nodes.length; i++) {
                    var n = nodes[i];
                    var text = (n.textContent || '').trim();
                    var tag = n.tagName;
                    var textLower = text.toLowerCase();

                    if (textLower === 'update') {
                        return n;
                    }

                    if ((tag === 'A' || tag === 'BUTTON') &&
                        textLower.indexOf('update') !== -1 &&
                        textLower.length <= 15) {
                        return n;
                    }

                    var attrs = [
                        n.getAttribute ? n.getAttribute('title') : '',
                        n.getAttribute ? n.getAttribute('data-original-title') : '',
                        n.getAttribute ? n.getAttribute('aria-label') : '',
                        n.getAttribute ? n.getAttribute('href') : '',
                        n.getAttribute ? n.getAttribute('onclick') : ''
                    ].join(' ').toLowerCase();

                    if (attrs.indexOf('update') !== -1 || attrs.indexOf('edit') !== -1) {
                        return n;
                    }
                }
                return null;
                """,
                row,
            )
            if el:
                return el
        except Exception:
            pass

        # 2) Selenium fallback
        for by, selector in strategies:
            try:
                el = row.find_element(by, selector)
                if el:
                    return el
            except Exception:
                pass

        time.sleep(0.3)

    return None


# ==== FIND MATCHING ROW (WAITS FOR TABLE) ====
def find_matching_row(driver, pod_no):
    """
    Wait for table rows, then return (row, dms_pod, cells) for the first POD match.
    Returns (None, None, None) if not found.
    """
    for _ in range(3):
        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_all_elements_located((By.XPATH, "//table/tbody/tr"))
            )
            rows = driver.find_elements(By.XPATH, "//table/tbody/tr")
            for row in rows:
                cells = row.find_elements(By.TAG_NAME, "td")
                if not cells:
                    continue
                dms_pod = cells[0].text.strip()
                if pod_matches(pod_no, dms_pod):
                    return row, dms_pod, cells
        except Exception:
            pass
        time.sleep(1)

    return None, None, None


# ==== CORE PROCESSING (WITH ROW VERIFICATION) ====
def process_loan(rec, worker_id, driver):
    """Process a single loan using an already logged-in driver."""
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
        except Exception as ex:
            print(f"[Worker-{worker_id}] [{loan_no}] Dispatch tab not found: {ex}")
            if document_type.upper() == "OTC":
                status_result = "NA"
            else:
                status_result = "NO"
            with lock:
                results[idx] = status_result
                updater_names[idx] = updater_name
            return

        # Parse date
        if '.' in date_txt:
            day, month, year = date_txt.split('.')
        elif '-' in date_txt:
            day, month, year = date_txt.split('-')
        else:
            raise ValueError(f"Unrecognized date format: {date_txt}")
        date_formatted = f"{day}/{month}/{year}"

        # ---- Loop with verification to ensure correct row ----
        max_attempts = 3
        for attempt in range(max_attempts):
            # (Re)acquire the row and its cells
            row, dms_pod, cells = find_matching_row(driver, pod_no)
            if not row:
                break

            # Find Update link inside this row
            update_link = find_update_element(driver, row)

            if update_link is None:
                print(f"[Worker-{worker_id}] [{loan_no}] Update link not found in row for POD {dms_pod} (attempt {attempt+1})")
                time.sleep(1)
                continue

            # ---- VERIFICATION: ensure the row still belongs to the correct POD ----
            try:
                first_cell_text = row.find_element(By.TAG_NAME, "td").text.strip()
            except Exception:
                print(f"[Worker-{worker_id}] [{loan_no}] Row became stale, re-acquiring...")
                time.sleep(1)
                continue

            if not pod_matches(pod_no, first_cell_text):
                print(f"[Worker-{worker_id}] [{loan_no}] Row POD changed from {dms_pod} to {first_cell_text}, re-acquiring...")
                time.sleep(1)
                continue

            # ---- Everything is correct, proceed to click and update ----
            print(f"[Worker-{worker_id}] [{loan_no}] Clicking Update on row with POD: {first_cell_text}")
            driver.execute_script("arguments[0].scrollIntoView(true);", update_link)
            update_link.click()
            time.sleep(2)

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

            print(f"[Worker-{worker_id}] [{loan_no}] POD {dms_pod} updated: '{remark_val}'")
            status_result = "YES"
            updater_name = "cb112700 (script)"
            break  # success, exit loop

        else:
            # Loop exhausted without success
            print(f"[Worker-{worker_id}] [{loan_no}] Failed to update after {max_attempts} attempts.")
            status_result = "NO"

    except Exception as e:
        print(f"[Worker-{worker_id}] [{loan_no}] Error: {type(e).__name__}: {e}")
        status_result = "NO"

    with lock:
        results[idx] = status_result
        updater_names[idx] = updater_name


# ==== WORKER FUNCTION (ONE LOGIN, ONE BROWSER PER WORKER) ====
def worker_function(records_batch, worker_id):
    """Each worker opens Chrome once, logs in once, reuses session for its batch."""
    if not records_batch:
        return

    print(f"[Worker-{worker_id}] Starting, {len(records_batch)} records")
    driver = webdriver.Chrome()

    try:
        # Login once
        driver.get("https://dcm.chola.murugappa.com/login")
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "username"))
        ).send_keys("cb112700")

        driver.find_element(By.ID, "password").send_keys("Naruto@@2005")

        WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.CLASS_NAME, "btn-primary"))
        ).click()

        time.sleep(3)

        # Process all records with this session
        for idx, rec in enumerate(records_batch, 1):
            print(f"[Worker-{worker_id}] {idx}/{len(records_batch)} -> {rec['loan_no']}")
            process_loan(rec, worker_id, driver)
            time.sleep(1)

        print(f"[Worker-{worker_id}] Completed.")

    finally:
        driver.quit()


# ==== SPLIT RECORDS ACROSS WORKERS ====
def split_records(all_records, num_workers):
    """Split records list into roughly equal batches."""
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


# ==== MAIN EXECUTION ====
if __name__ == "__main__":
    start_time = time.time()

    batches = split_records(records, NUM_WORKERS)

    with ThreadPoolExecutor(max_workers=len(batches)) as executor:
        futures = []
        for worker_id, batch in enumerate(batches, 1):
            if batch:
                futures.append(executor.submit(worker_function, batch, worker_id))

        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as e:
                print(f"Worker error: {e}")

    # Write outputs IN ORDER
    with open(status_file, 'w', encoding='utf-8') as sf:
        for rec in records:
            idx = rec['index']
            loan = rec['loan_no']
            status = results.get(idx, "ERROR")
            name = updater_names.get(idx, "")
            sf.write(f"{loan}\t{status}\t{name}\n")

    # Write not found file (order by original index)
    with open(notfound_file, 'w', encoding='utf-8') as nf:
        for rec in records:
            idx = rec['index']
            status = results.get(idx, "ERROR")

            if status == "NO":
                nf.write(f"{rec['loan_no']}\n")
            elif status == "NA":
                nf.write(f"{rec['loan_no']}\tOTC-NA\n")

    elapsed = time.time() - start_time

    # Count stats
    yes_count = sum(1 for s in results.values() if s == "YES")
    no_count = sum(1 for s in results.values() if s == "NO")
    na_count = sum(1 for s in results.values() if s == "NA")

    print("\n" + "=" * 70)
    print("SCRIPT COMPLETED")
    print("=" * 70)
    print(f"Total records       : {len(records)}")
    print(f"Successfully updated: {yes_count}")
    print(f"Not found / failed  : {no_count}")
    print(f"OTC - NA            : {na_count}")
    print(f"Workers used        : {len(batches)}")
    print(f"Time taken (sec)    : {elapsed:.2f}")
    print("Check status.txt and notfound.txt on Desktop.")
    print("=" * 70)