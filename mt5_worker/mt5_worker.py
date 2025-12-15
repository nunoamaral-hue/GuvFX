import os
import time
import requests


API_BASE = os.getenv("GUVFX_API_BASE", "http://127.0.0.1:8000/api")
WORKER_TOKEN = os.getenv("MT5_WORKER_TOKEN")
WORKER_ID = os.getenv("MT5_WORKER_ID", "mt5-worker-1")


def get_next_job():
    url = f"{API_BASE}/execution/jobs/next/"
    headers = {
        "X-Worker-Token": WORKER_TOKEN or "",
        # If you decide to also use JWT for the worker, you'd add it here.
    }
    params = {"worker_id": WORKER_ID}
    resp = requests.get(url, headers=headers, params=params, timeout=10)
    if resp.status_code == 204:
        return None
    resp.raise_for_status()
    return resp.json()


def get_account_credentials(account_id: int) -> dict:
    url = f"{API_BASE}/execution/accounts/{account_id}/credentials/"
    headers = {
        "X-Worker-Token": WORKER_TOKEN or "",
    }
    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


def complete_job(job_id: int, status: str, result: dict | None = None,
                 error_message: str = ""):
    url = f"{API_BASE}/execution/jobs/{job_id}/complete/"
    headers = {
        "X-Worker-Token": WORKER_TOKEN or "",
    }
    payload = {
        "status": status,
        "result": result or {},
        "error_message": error_message,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    resp.raise_for_status()


"""def test_mt5_connection(account: dict) -> tuple[bool, str]:
    server = account["broker_name"]
    login = int(account["login"])
    password = account["password"]

    if not mt5.initialize(server=server):
        err = mt5.last_error()
        mt5.shutdown()
        return False, f"initialize failed: {err}"

    authorized = mt5.login(login=login, password=password, server=server)
    if not authorized:
        err = mt5.last_error()
        mt5.shutdown()
        return False, f"login failed: {err}"

    mt5.shutdown()
    return True, "MT5 connection successful"
"""
def test_mt5_connection(account: dict) -> tuple[bool, str]:
    print(f"Would test MT5 connection for account {account['id']}")
    return True, "Dummy success in dev"

def process_job(job: dict):
    job_id = job["id"]
    job_type = job["job_type"]
    account_id = job["account"]

    try:
        account = get_account_credentials(account_id)
    except Exception as e:
        complete_job(job_id, "FAILED", result={}, error_message=f"Creds error: {e}")
        return

    if job_type == "TEST_CONNECTION":
        ok, msg = test_mt5_connection(account)
        if ok:
            complete_job(job_id, "SUCCESS", result={"message": msg}, error_message="")
        else:
            complete_job(job_id, "FAILED", result={"message": msg}, error_message=msg)
    elif job_type == "OPEN_TRADE":
        payload = job.get("payload") or {}
        ok, msg, ticket = open_mt5_order_dummy(payload, account)
        result = {"message": msg}
        if ticket is not None:
            result["ticket"] = ticket
        status = "SUCCESS" if ok else "FAILED"
        complete_job(job_id, status, result=result, error_message=("" if ok else msg))
    else:
        complete_job(
            job_id,
            "FAILED",
            result={"message": f"Unsupported job_type {job_type}"},
            error_message="Unsupported job type for this worker",
        )

def open_mt5_order_dummy(payload: dict, account: dict) -> tuple[bool, str, int | None]:
    """
    Dev/dummy implementation for OPEN_TRADE jobs.

    On macOS (no real MetaTrader5), we just log what would have been sent
    and pretend success.
    """
    print("[DEV] Would open MT5 order with payload:")
    print(f"       account_id={account['id']}, server={account['broker_name']}, login={account['login']}")
    print(f"       payload={payload}")
    # Return a fake ticket ID
    return True, "Dummy order success (no real MT5 call)", 123456

def main_loop():
    if not WORKER_TOKEN:
        raise RuntimeError("MT5_WORKER_TOKEN is not set for the worker")

    while True:
        try:
            job = get_next_job()
            if not job:
                time.sleep(2)
                continue
            process_job(job)
        except requests.HTTPError as e:
            # Network/API errors – you might want to log these somewhere more persistent
            print(f"[ERROR] HTTP error: {e}")
            time.sleep(5)
        except Exception as e:
            print(f"[ERROR] Unhandled exception in worker loop: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main_loop()