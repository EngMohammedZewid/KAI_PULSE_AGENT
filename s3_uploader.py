"""
S3 File Uploader — local GUI utility
=====================================
Opens a file picker dialog, uploads selected files to your AWS S3 bucket,
and shows a summary.  Uses only Python stdlib (tkinter + urllib) — no boto3.

Usage:
    python s3_uploader.py [--folder s3/prefix/]

After upload, use the files in Orchestrate chat with:
    render_chart_from_s3 / inspect_s3_file / list_s3_files
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import hmac
import mimetypes
import os
import pathlib
import sys
import threading
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
from tkinter import filedialog, messagebox, ttk


# ---------------------------------------------------------------------------
# Credentials — reads from .env in same directory, then falls back to env vars
# ---------------------------------------------------------------------------

def _load_env() -> dict[str, str]:
    """Load .env from the script directory, return as dict."""
    env: dict[str, str] = {}
    env_path = pathlib.Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _get_creds() -> tuple[str, str, str, str]:
    """Return (access_key, secret_key, bucket, region) from .env or env vars."""
    loaded = _load_env()

    def g(key: str) -> str:
        return loaded.get(key) or os.environ.get(key, "")

    access_key = g("AWS_ACCESS_KEY_ID")
    secret_key = g("AWS_SECRET_ACCESS_KEY")
    bucket = g("AWS_S3_BUCKET")
    region = g("AWS_S3_REGION") or "us-east-1"

    missing = [k for k, v in {
        "AWS_ACCESS_KEY_ID": access_key,
        "AWS_SECRET_ACCESS_KEY": secret_key,
        "AWS_S3_BUCKET": bucket,
    }.items() if not v]
    if missing:
        raise RuntimeError(f"Missing credentials: {', '.join(missing)}")
    return access_key, secret_key, bucket, region


# ---------------------------------------------------------------------------
# AWS SigV4 helpers
# ---------------------------------------------------------------------------

def _hmac_sha256(key: bytes, data: str) -> bytes:
    return hmac.new(key, data.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret_key: str, date_stamp: str, region: str) -> bytes:
    k = _hmac_sha256(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k = _hmac_sha256(k, region)
    k = _hmac_sha256(k, "s3")
    k = _hmac_sha256(k, "aws4_request")
    return k


def _s3_put(
    file_key: str,
    body: bytes,
    content_type: str,
    access_key: str,
    secret_key: str,
    bucket: str,
    region: str,
) -> None:
    """PUT a file to S3 using SigV4 signing. Raises on error."""
    host = f"{bucket}.s3.{region}.amazonaws.com"
    t = datetime.datetime.utcnow()
    amz_date = t.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = t.strftime("%Y%m%d")

    canonical_uri = "/" + urllib.parse.quote(file_key.lstrip("/"), safe="/")
    payload_hash = hashlib.sha256(body).hexdigest()

    canonical_headers = (
        f"content-type:{content_type}\n"
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
    )
    signed_headers = "content-type;host;x-amz-content-sha256;x-amz-date"

    canonical_request = "\n".join([
        "PUT",
        canonical_uri,
        "",  # no query string
        canonical_headers,
        signed_headers,
        payload_hash,
    ])

    credential_scope = f"{date_stamp}/{region}/s3/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    sig_key = _signing_key(secret_key, date_stamp, region)
    signature = hmac.new(sig_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    url = f"https://{host}{canonical_uri}"
    req = urllib.request.Request(
        url,
        method="PUT",
        data=body,
        headers={
            "Content-Type": content_type,
            "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_hash,
            "Authorization": authorization,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120):
            pass
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"S3 HTTP {e.code}: {err}") from e


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class S3UploaderApp(tk.Tk):
    def __init__(self, s3_folder: str = "") -> None:
        super().__init__()
        self.title("S3 File Uploader")
        self.resizable(True, True)
        self.minsize(640, 480)
        self._s3_folder = s3_folder.strip("/")
        self._files: list[str] = []
        self._build_ui()
        self._load_creds()

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        # --- Credentials row ---
        cred_frame = ttk.LabelFrame(self, text="AWS Credentials (auto-loaded from .env)")
        cred_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        cred_frame.columnconfigure(1, weight=1)
        cred_frame.columnconfigure(3, weight=1)

        ttk.Label(cred_frame, text="Bucket:").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self._var_bucket = tk.StringVar()
        ttk.Entry(cred_frame, textvariable=self._var_bucket, width=30).grid(row=0, column=1, sticky="ew", padx=4)

        ttk.Label(cred_frame, text="Region:").grid(row=0, column=2, sticky="w", padx=(12, 4))
        self._var_region = tk.StringVar()
        ttk.Entry(cred_frame, textvariable=self._var_region, width=14).grid(row=0, column=3, sticky="ew", padx=4)

        # --- S3 folder row ---
        folder_frame = ttk.Frame(self)
        folder_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=4)
        folder_frame.columnconfigure(1, weight=1)

        ttk.Label(folder_frame, text="S3 folder prefix (optional):").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self._var_folder = tk.StringVar(value=self._s3_folder)
        ttk.Entry(folder_frame, textvariable=self._var_folder).grid(row=0, column=1, sticky="ew")
        ttk.Label(folder_frame, text='e.g.  "reports/"  or leave blank', foreground="gray").grid(
            row=0, column=2, sticky="w", padx=(8, 0)
        )

        # --- Buttons ---
        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=2, column=0, sticky="ew", padx=10, pady=4)

        self._btn_select = ttk.Button(btn_frame, text="📂  Select Files…", command=self._select_files)
        self._btn_select.pack(side="left", padx=(0, 6))

        self._btn_upload = ttk.Button(btn_frame, text="⬆  Upload to S3", command=self._start_upload, state="disabled")
        self._btn_upload.pack(side="left", padx=(0, 6))

        self._btn_clear = ttk.Button(btn_frame, text="✖  Clear", command=self._clear_files, state="disabled")
        self._btn_clear.pack(side="left")

        # --- File list ---
        list_frame = ttk.LabelFrame(self, text="Selected files")
        list_frame.grid(row=3, column=0, sticky="nsew", padx=10, pady=4)
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        cols = ("Local file", "S3 key", "Size", "Status")
        self._tree = ttk.Treeview(list_frame, columns=cols, show="headings", selectmode="extended")
        for col, w in zip(cols, (280, 240, 80, 120)):
            self._tree.heading(col, text=col)
            self._tree.column(col, width=w, minwidth=60)
        self._tree.grid(row=0, column=0, sticky="nsew")

        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self._tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        self._tree.configure(yscrollcommand=vsb.set)

        self._tree.tag_configure("ok", foreground="green")
        self._tree.tag_configure("err", foreground="red")
        self._tree.tag_configure("busy", foreground="blue")

        # --- Progress bar ---
        self._progress = ttk.Progressbar(self, orient="horizontal", mode="determinate")
        self._progress.grid(row=4, column=0, sticky="ew", padx=10, pady=(2, 2))

        # --- Status label ---
        self._var_status = tk.StringVar(value="Ready. Select files to upload.")
        ttk.Label(self, textvariable=self._var_status, anchor="w").grid(
            row=5, column=0, sticky="ew", padx=10, pady=(0, 8)
        )

    # ── Credential loading ───────────────────────────────────────────────────

    def _load_creds(self) -> None:
        try:
            _, _, bucket, region = _get_creds()
            self._var_bucket.set(bucket)
            self._var_region.set(region)
        except RuntimeError as e:
            self._var_status.set(f"Warning: {e}  —  fill in bucket/region manually above.")

    # ── File selection ───────────────────────────────────────────────────────

    def _select_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Select files to upload to S3",
            filetypes=[
                ("All files", "*.*"),
                ("CSV files", "*.csv"),
                ("JSON files", "*.json"),
                ("Text files", "*.txt"),
                ("Excel files", "*.xlsx *.xls"),
            ],
        )
        if not paths:
            return

        for path in paths:
            if path in self._files:
                continue
            self._files.append(path)
            name = pathlib.Path(path).name
            folder = self._var_folder.get().strip("/")
            s3_key = f"{folder}/{name}" if folder else name
            size = pathlib.Path(path).stat().st_size
            size_str = f"{size / 1024:.1f} KB" if size < 1_048_576 else f"{size / 1_048_576:.1f} MB"
            self._tree.insert("", "end", iid=path, values=(path, s3_key, size_str, "Pending"))

        count = len(self._files)
        self._btn_upload.config(state="normal" if count else "disabled")
        self._btn_clear.config(state="normal" if count else "disabled")
        self._var_status.set(f"{count} file(s) selected. Click 'Upload to S3' when ready.")

    def _clear_files(self) -> None:
        self._files.clear()
        for item in self._tree.get_children():
            self._tree.delete(item)
        self._btn_upload.config(state="disabled")
        self._btn_clear.config(state="disabled")
        self._progress["value"] = 0
        self._var_status.set("Ready. Select files to upload.")

    # ── Upload logic ─────────────────────────────────────────────────────────

    def _start_upload(self) -> None:
        if not self._files:
            return
        self._btn_select.config(state="disabled")
        self._btn_upload.config(state="disabled")
        self._btn_clear.config(state="disabled")
        t = threading.Thread(target=self._upload_all, daemon=True)
        t.start()

    def _upload_all(self) -> None:
        try:
            access_key, secret_key, _, _ = _get_creds()
        except RuntimeError as e:
            self.after(0, lambda: messagebox.showerror("Credentials error", str(e)))
            self.after(0, self._re_enable_buttons)
            return

        bucket = self._var_bucket.get().strip()
        region = self._var_region.get().strip() or "us-east-1"
        folder = self._var_folder.get().strip("/")

        if not bucket:
            self.after(0, lambda: messagebox.showerror("Missing bucket", "Please enter the S3 bucket name."))
            self.after(0, self._re_enable_buttons)
            return

        total = len(self._files)
        ok = 0
        fail = 0

        for i, path in enumerate(self._files):
            name = pathlib.Path(path).name
            s3_key = f"{folder}/{name}" if folder else name
            self.after(0, lambda p=path, k=s3_key: self._set_row_status(p, k, "Uploading…", "busy"))
            self.after(0, lambda v=i: self._set_progress(v, total))

            try:
                body = pathlib.Path(path).read_bytes()
                ct = mimetypes.guess_type(path)[0] or "application/octet-stream"
                _s3_put(s3_key, body, ct, access_key, secret_key, bucket, region)
                self.after(0, lambda p=path, k=s3_key: self._set_row_status(p, k, "✓ Uploaded", "ok"))
                ok += 1
            except Exception as e:
                self.after(0, lambda p=path, k=s3_key, err=str(e): self._set_row_status(p, k, f"✗ {err[:60]}", "err"))
                fail += 1

        self.after(0, lambda: self._set_progress(total, total))
        self.after(0, lambda: self._var_status.set(
            f"Done — {ok} uploaded, {fail} failed.  "
            f"Use inspect_s3_file or list_s3_files in Orchestrate chat."
        ))
        self.after(0, self._re_enable_buttons)

    def _set_row_status(self, iid: str, s3_key: str, status: str, tag: str) -> None:
        if self._tree.exists(iid):
            vals = list(self._tree.item(iid, "values"))
            vals[1] = s3_key  # refresh S3 key in case folder changed
            vals[3] = status
            self._tree.item(iid, values=vals, tags=(tag,))

    def _set_progress(self, done: int, total: int) -> None:
        self._progress["maximum"] = total
        self._progress["value"] = done

    def _re_enable_buttons(self) -> None:
        self._btn_select.config(state="normal")
        self._btn_clear.config(state="normal")
        # Only re-enable upload if files remain
        if self._files:
            self._btn_upload.config(state="normal")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Upload files to S3 via GUI file picker")
    parser.add_argument("--folder", default="", help="S3 folder prefix, e.g. 'reports/'")
    args = parser.parse_args()

    app = S3UploaderApp(s3_folder=args.folder)
    app.mainloop()
