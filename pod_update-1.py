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
# alongside the Received At date match. Matching tolerates 1-2 letter typos,
# as well as nickname/full-name containment (e.g. RAGU -> RAGUPATHI).
TEAM_NAMES = [
    "INFANT", "DINESH", "DURGA", "ELUMALAI", "FEROZ", "PRIYA",
    "SATISH", "SNEHA", "SOWMIYA", "SRINATH", "THIRU", "LOKESH", "PREETI",
    "RAGU", "RAGUPATHI",
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
    Fuzzy-match the 'Updated By' name against TEAM_NAMES. Two ways to match:
    1) Typo tolerance: 1-2 letter spelling differences (e.g. 'Feroz Khan
       (CB99354)' -> FEROZ, or 'Frooz' -> FEROZ).
    2) Nickname / full-name containment: e.g. 'Ragupathi Rajabommannan
       (CB112700)' -> RAGU, since RAGU is a short nickname for RAGUPATHI.
    Returns (True, matched_team_name) or (False, None).
    """
    if not updated_by_text:
        return False, None
    # Drop trailing employee code like "(CB99354)" and split into words
    name_part = updated_by_text.split('(')[0].strip().upper()
    words = [w for w in name_part.split() if w]
    for word in words:
        for team_name in TEAM_NAMES:
            if levenshtein(word, team_name) <= max_distance:
                return True, team_name
            # Nickname/full-name containment (only for names of reasonable
            # length, to avoid short substrings causing false positives)
            if len(team_name) >= 4 and (team_name in word or word in team_name):
                return True, team_name
    return False, None


# Shared data structures with thread-safe lock
# Using a dictionary to store results by index to preserve order
results = {}  # Format: {index: 'YES'/'NO'/'NA'}
updater_names = {}  # Format: {index: 'name who updated / blank'}
lock = threading.Lock()

# ==== READ INPUT RECORDS ====
records = []
original_lines = []  # To keep track of input order
with open(input_file, 'r', encoding='utf-8') as f:
    for idx, line in enumerate(f):
        original_lines.append(line.strip())
        parts = line.strip().split('\t')
        if len(parts) >= 9:
            date_txt = parts[0]
            pod_no = parts[4]
            document_type = parts[5].strip()  # e.g., 'PDD', 'OTC', 'FILE AND DOCKET'
            orig_status = parts[6].strip()  # 'WITH ORGINAL' or 'WITHOUT ORGINAL'
            loan_no = parts[-1]
            remark_val = f"{document_type} {orig_status}"  # for Remarks
            records.append({
                'index': idx,  # Store original index
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
    
    # Exact match
    if input_pod == dms_pod:
        return True
    
    # One is contained in the other
    if input_pod in dms_pod or dms_pod in input_pod:
        return True
    
    # Find longest common substring
    longer = input_pod if len(input_pod) >= len(dms_pod) else dms_pod
    shorter = dms_pod if len(input_pod) >= len(dms_pod) else input_pod
    
    if len(shorter) >= min_match_length:
        for i in range(len(shorter) - min_match_length + 1):
            substring = shorter[i:i + min_match_length]
            if substring in longer:
                return True
    
    return False

# ==== ROBUST "UPDATE" ELEMENT LOCATOR ====
def find_update_element(row):
    """
    Locate the row's Update action robustly. By.LINK_TEXT requires an exact,
    case-sensitive match on an <a> tag only - it silently fails (and gets
    misread as 'already updated') if the element is a <button>, has extra
    whitespace/icon text, or different casing. Try several strategies instead.
    """
    strategies = [
        (By.LINK_TEXT, "Update"),
        (By.PARTIAL_LINK_TEXT, "Update"),
        (By.XPATH, ".//a[normalize-space()='Update']"),
        (By.XPATH, ".//a[contains(normalize-space(.), 'Update')]"),
        (By.XPATH, ".//button[contains(normalize-space(.), 'Update')]"),
        (By.XPATH, ".//*[self::a or self::button or self::span]"
                   "[contains(translate(normalize-space(.), "
                   "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'update')]"),
    ]
    for by, selector in strategies:
        try:
            el = row.find_element(by, selector)
            if el:
                return el
        except (NoSuchElementException, StaleElementReferenceException):
            continue
    return None

# ==== CORE PROCESSING (USES EXISTING LOGGED-IN DRIVER) ====
def process_loan(rec, worker_id, driver):
    """Process a single loan using an already logged-in driver."""
    idx = rec['index']
    loan_no = rec['loan_no']
    pod_no = rec['pod_no']
    date_txt = rec['date_txt']
    remark_val = rec['remark']
    document_type = rec['document_type']
    
    status_result = "NO"  # Default
    updater_name = ""  # Who performed/already did the update
    
    try:
        loan_url = f"https://dcm.chola.murugappa.com/loan-details/{loan_no}"
        driver.get(loan_url)
        time.sleep(2)
        
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
        
        found = False
        table_rows = driver.find_elements(By.XPATH, "//table/tbody/tr")
        
        for row in table_rows:
            cells = row.find_elements(By.TAG_NAME, "td")
            if cells:
                dms_pod = cells[0].text.strip()
                
                # Use flexible matching for POD numbers
                if pod_matches(pod_no, dms_pod):
                    found = True
                    print(f"[Worker-{worker_id}] [{loan_no}] POD matched: Input={pod_no}, DMS={dms_pod}")
                    
                    # Accept date_txt in either 'DD.MM.YYYY' or 'DD-MM-YYYY' format
                    if '.' in date_txt:
                        day, month, year = date_txt.split('.')
                    elif '-' in date_txt:
                        day, month, year = date_txt.split('-')
                    else:
                        raise ValueError(f"Unrecognized date format: {date_txt}")
                    date_formatted = f"{day}/{month}/{year}"
                    
                    # Try to find and click Update link
                    try:
                        update_link = find_update_element(row)
                        if update_link is None:
                            raise NoSuchElementException(
                                "Update element not found via any locator strategy"
                            )
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
                            
                    except Exception as e:
                        # Fell back here because the Update click/flow raised an
                        # exception - could genuinely mean "already updated", or
                        # could mean the locator/click failed for another reason.
                        # Log the real exception so misclassifications are visible.
                        print(f"[Worker-{worker_id}] [{loan_no}] POD {dms_pod} - Update flow exception: {type(e).__name__}: {e}")
                        # Don't trust the name alone (may be wrong/blank for non-team
                        # members), and don't trust just "any date present" either.
                        # Require BOTH: Received At date matches 1.txt's date, AND
                        # the Updated By name fuzzy-matches a known team member.
                        try:
                            if len(cells) > 7:
                                updated_by_text = cells[6].text.strip()
                                received_at_text = cells[7].text.strip()
                                
                                # Treat blank or '-' as "not actually updated"
                                received_at_filled = bool(received_at_text) and received_at_text != "-"
                                date_ok = received_at_filled and received_at_text == date_formatted
                                member_ok, matched_team_name = is_team_member(updated_by_text)
                                
                                if date_ok and member_ok:
                                    print(f"[Worker-{worker_id}] [{loan_no}] POD {dms_pod} Received At='{received_at_text}' matches expected '{date_formatted}' AND updater '{updated_by_text}' matches team member '{matched_team_name}' - marking as YES")
                                    status_result = "YES"
                                    updater_name = updated_by_text if updated_by_text else "(name blank)"
                                elif date_ok and not member_ok:
                                    print(f"[Worker-{worker_id}] [{loan_no}] POD {dms_pod} - Date matches but updater '{updated_by_text}' not recognized as team member - marking as NO")
                                    status_result = "NO"
                                elif received_at_filled:
                                    print(f"[Worker-{worker_id}] [{loan_no}] POD {dms_pod} - Received At='{received_at_text}' does NOT match expected '{date_formatted}' - marking as NO")
                                    status_result = "NO"
                                else:
                                    print(f"[Worker-{worker_id}] [{loan_no}] POD {dms_pod} - Received At not filled ('{received_at_text}') - marking as NO")
                                    status_result = "NO"
                            else:
                                print(f"[Worker-{worker_id}] [{loan_no}] POD {dms_pod} - Cannot verify Received At column")
                                status_result = "NO"
                                    
                        except Exception as check_ex:
                            print(f"[Worker-{worker_id}] [{loan_no}] Update/entry problem: {e}")
                            status_result = "NO"
                    
                    break
        
        if not found:
            if document_type.upper() == "OTC":
                print(f"[Worker-{worker_id}] [{loan_no}] POD {pod_no} not found - OTC marked as NA.")
                status_result = "NA"
            else:
                print(f"[Worker-{worker_id}] [{loan_no}] POD {pod_no} not found.")
                status_result = "NO"
                
    except Exception as e:
        print(f"[Worker-{worker_id}] Error processing loan {loan_no}: {e}")
        if document_type.upper() == "OTC":
            status_result = "NA"
        else:
            status_result = "NO"
            
    with lock:
        results[idx] = status_result
        updater_names[idx] = updater_name

# ==== WORKER FUNCTION (ONE LOGIN, ONE BROWSER PER WORKER) ====
# ==== LOGIN WITH RETRY (EXPONENTIAL BACKOFF) ====
def login_with_retry(driver, worker_id, max_retries=3):
    """
    Attempt login up to max_retries times. Waits 2s, 4s, 8s... between
    attempts (exponential backoff) to ride out transient network/server
    hiccups instead of failing the whole worker on one bad attempt.
    """
    for attempt in range(1, max_retries + 1):
        try:
            driver.get("https://dcm.chola.murugappa.com/login")
            
            username_field = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.ID, "username"))
            )
            username_field.clear()
            username_field.send_keys("cb112700")
            
            password_field = driver.find_element(By.ID, "password")
            password_field.clear()
            password_field.send_keys("Naruto@@2005")
            
            WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.CLASS_NAME, "btn-primary"))
            ).click()
            
            # Confirm login actually succeeded (redirected off /login,
            # or dashboard element present) before declaring success.
            WebDriverWait(driver, 10).until(
                EC.invisibility_of_element_located((By.ID, "username"))
            )
            
            print(f"[Worker-{worker_id}] Login successful (attempt {attempt}/{max_retries})")
            return True
        
        except Exception as e:
            print(f"[Worker-{worker_id}] Login attempt {attempt}/{max_retries} failed: {e}")
            if attempt < max_retries:
                backoff = 2 ** attempt  # 2s, 4s, 8s...
                print(f"[Worker-{worker_id}] Retrying login in {backoff}s...")
                time.sleep(backoff)
            else:
                print(f"[Worker-{worker_id}] Login failed after {max_retries} attempts - giving up")
                return False
    return False

def worker_function(records_batch, worker_id):
    """Each worker opens Chrome once, logs in once, reuses session for its batch."""
    if not records_batch:
        return
    
    print(f"[Worker-{worker_id}] Starting, {len(records_batch)} records")
    driver = webdriver.Chrome()
    
    try:
        # Login once, with retry/backoff
        if not login_with_retry(driver, worker_id, max_retries=3):
            print(f"[Worker-{worker_id}] Aborting - could not log in.")
            return
        
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
        # Iterate through original records order
        for rec in records:
            idx = rec['index']
            loan = rec['loan_no']
            status = results.get(idx, "ERROR")  # Default if missing
            name = updater_names.get(idx, "")
            sf.write(f"{loan}\t{status}\t{name}\n")
    
    # Write not found file (still grouped for convenience, or could be ordered if needed)
    with open(notfound_file, 'w', encoding='utf-8') as nf:
        # Order notfound entries by original index too
        sorted_results = sorted(results.items())
        for idx, status in sorted_results:
            if status == "NO":
                # Find corresponding loan number
                loan = records[idx]['loan_no'] # records list index matches simplified index logic if no filtering happened, 
                                               # but safer to find by stored index
                # Actually records list might be shorter than file lines if some skipped.
                # Let's map back correctly.
                # records list only contains processed lines.
                # We need to find the record with this index.
                rec = next((r for r in records if r['index'] == idx), None)
                if rec:
                    nf.write(f"{rec['loan_no']}\n")
            elif status == "NA":
                rec = next((r for r in records if r['index'] == idx), None)
                if rec:
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
