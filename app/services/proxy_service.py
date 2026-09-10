from __future__ import annotations

from urllib.parse import parse_qs, urlparse
from urllib.request import Request as UrlRequest, urlopen
from urllib.error import HTTPError

from sqlalchemy.orm import Session

from app.models import AccountProxy, SystemKV


SUPPORTED_PROXY_TYPES = {"socks5", "http"}
DEFAULT_PROXY_SOURCE = "http://global.rotgbapi.711proxy.com:8089/gen?zone=custom&ptype=1&count=900&proto=http&stype=text&split=%0D%0A&sessType=rotating"


def _normalize_proxy_type(value: str | None) -> str:
    proxy_type = (value or "socks5").strip().lower()
    if proxy_type not in SUPPORTED_PROXY_TYPES:
        raise ValueError("unsupported_proxy_type")
    return proxy_type


def _clean_raw_proxy_value(raw_value: str | None) -> str:
    value = (raw_value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"`", '"', "'"}:
        value = value[1:-1].strip()
    return value


def infer_proxy_type_from_source(source_value: str | None, fallback: str = "socks5") -> str:
    value = _clean_raw_proxy_value(source_value)
    if not value:
        return _normalize_proxy_type(fallback)
    try:
        parsed = urlparse(value)
    except Exception:
        return _normalize_proxy_type(fallback)

    query = parse_qs(parsed.query or "", keep_blank_values=True)
    for key in ("proto", "protocol", "proxy_type", "ptype_name", "type"):
        raw = (query.get(key) or [None])[0]
        if raw in SUPPORTED_PROXY_TYPES:
            return _normalize_proxy_type(raw)

    lowered = value.lower()
    if "socks5" in lowered:
        return "socks5"
    if "proto=http" in lowered:
        return "http"
    return _normalize_proxy_type(fallback)


def parse_proxy_input(raw_value: str | None, default_proxy_type: str = "socks5") -> dict | None:
    value = _clean_raw_proxy_value(raw_value)
    if not value:
        return None

    proxy_type = _normalize_proxy_type(default_proxy_type)
    username = None
    password = None
    host = None
    port = None

    if "://" in value:
        parsed = urlparse(value)
        proxy_type = _normalize_proxy_type(parsed.scheme)
        # A usable proxy must be just scheme://host:port; source URLs with path/query
        # are proxy providers' APIs and must not be stored as account bindings.
        if parsed.path not in ("", "/") or parsed.query or parsed.params or parsed.fragment:
            raise ValueError("invalid_proxy_endpoint")
        host = parsed.hostname
        port = parsed.port
        username = parsed.username
        password = parsed.password
    else:
        parts = [part.strip() for part in value.split(":")]
        if len(parts) == 2:
            host, port = parts
        elif len(parts) == 4:
            host, port, username, password = parts
        else:
            raise ValueError("invalid_proxy_format")

    host = (host or "").strip()
    if not host:
        raise ValueError("missing_proxy_host")

    try:
        port = int(port)
    except Exception as exc:
        raise ValueError("invalid_proxy_port") from exc

    if port <= 0 or port > 65535:
        raise ValueError("invalid_proxy_port")

    return {
        "proxy_type": proxy_type,
        "host": host,
        "port": port,
        "username": username or None,
        "password": password or None,
        "raw_input": value,
        "enabled": 1,
    }


def normalize_proxy_input(raw_value: str | None, default_proxy_type: str = "socks5") -> str | None:
    parsed = parse_proxy_input(raw_value, default_proxy_type=default_proxy_type)
    if parsed is None:
        return None
    return format_proxy_url(parsed, mask_password=False)


def fetch_proxy_lines_from_url(
    source_url: str,
    *,
    timeout: int = 20,
    default_proxy_type: str = "socks5",
) -> list[str]:
    cleaned_url = _clean_raw_proxy_value(source_url)
    req = UrlRequest(cleaned_url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8", errors="ignore")
    except HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="ignore").strip()
        except Exception:
            body = ""
        if body:
            raise RuntimeError(f"http_{exc.code}: {body}") from exc
        raise
    lines: list[str] = []
    for item in payload.splitlines():
        normalized = normalize_proxy_input(item, default_proxy_type=default_proxy_type)
        if normalized:
            lines.append(normalized)
    return lines


def resolve_proxy_source_text(
    raw_text: str | None,
    errors: list[str] | None = None,
) -> tuple[dict[str, str], list[str], dict]:
    issues = errors if errors is not None else []
    text = _clean_raw_proxy_value(raw_text)
    explicit_map: dict[str, str] = {}
    proxy_pool: list[str] = []
    meta = {
        "source_lines": 0,
        "remote_sources": 0,
        "pool_total": 0,
        "explicit_total": 0,
    }
    if not text:
        return explicit_map, proxy_pool, meta

    expanded_lines: list[tuple[str, str]] = []
    for raw_line in text.splitlines():
        line = _clean_raw_proxy_value(raw_line)
        if not line:
            continue
        meta["source_lines"] += 1
        if line.startswith("http://") or line.startswith("https://"):
            default_proxy_type = infer_proxy_type_from_source(line)
            try:
                remote_lines = fetch_proxy_lines_from_url(
                    line,
                    timeout=20,
                    default_proxy_type=default_proxy_type,
                )
                if not remote_lines:
                    issues.append(f"代理源为空: {line}")
                    continue
                meta["remote_sources"] += 1
                expanded_lines.extend((item, default_proxy_type) for item in remote_lines)
            except Exception as exc:
                err_text = str(exc)[:180]
                if "ip whitelist check failed" in err_text.lower():
                    issues.append(f"代理源拉取失败: 711Proxy 白名单校验失败，请把当前出口 IP 加入 711Proxy Whitelist 后重试")
                else:
                    issues.append(f"代理源拉取失败: {line} - {err_text}")
            continue
        expanded_lines.append((line, "socks5"))

    seen_pool: set[str] = set()
    for line, default_proxy_type in expanded_lines:
        if "|" in line:
            account, proxy_value = [part.strip() for part in line.split("|", 1)]
            if not account or not proxy_value:
                issues.append(f"代理映射格式错误: {line}")
                continue
            try:
                normalized = normalize_proxy_input(proxy_value, default_proxy_type=default_proxy_type)
            except ValueError as exc:
                issues.append(f"{account}: 代理格式错误 - {str(exc)}")
                continue
            if normalized:
                explicit_map[account] = normalized
            continue

        try:
            normalized = normalize_proxy_input(line, default_proxy_type=default_proxy_type)
        except ValueError as exc:
            issues.append(f"代理格式错误: {line} - {str(exc)}")
            continue
        if not normalized or normalized in seen_pool:
            continue
        seen_pool.add(normalized)
        proxy_pool.append(normalized)

    meta["pool_total"] = len(proxy_pool)
    meta["explicit_total"] = len(explicit_map)
    return explicit_map, proxy_pool, meta


def assign_proxy_pool_to_accounts(
    accounts: list[str],
    explicit_map: dict[str, str] | None,
    proxy_pool: list[str] | None,
) -> tuple[dict[str, str], dict]:
    ordered_accounts: list[str] = []
    seen_accounts: set[str] = set()
    for item in accounts or []:
        account = (item or "").strip()
        if not account or account in seen_accounts:
            continue
        seen_accounts.add(account)
        ordered_accounts.append(account)

    bindings: dict[str, str] = {}
    remaining_pool: list[str] = []
    seen_pool: set[str] = set()
    for item in proxy_pool or []:
        value = _clean_raw_proxy_value(item)
        if not value or value in seen_pool:
            continue
        seen_pool.add(value)
        remaining_pool.append(value)

    explicit_map = explicit_map or {}
    assigned_from_explicit = 0
    assigned_from_pool = 0
    unassigned_accounts: list[str] = []
    for account in ordered_accounts:
        explicit_value = _clean_raw_proxy_value(explicit_map.get(account))
        if explicit_value:
            bindings[account] = explicit_value
            assigned_from_explicit += 1
            continue
        if remaining_pool:
            bindings[account] = remaining_pool.pop(0)
            assigned_from_pool += 1
            continue
        unassigned_accounts.append(account)

    return bindings, {
        "accounts_total": len(ordered_accounts),
        "assigned_total": len(bindings),
        "assigned_from_explicit": assigned_from_explicit,
        "assigned_from_pool": assigned_from_pool,
        "unassigned_accounts": unassigned_accounts,
        "remaining_proxy_pool": len(remaining_pool),
    }


def format_proxy_url(proxy: AccountProxy | dict | None, mask_password: bool = True) -> str | None:
    if not proxy:
        return None

    proxy_type = (proxy.get("proxy_type") if isinstance(proxy, dict) else proxy.proxy_type) or "socks5"
    host = proxy.get("host") if isinstance(proxy, dict) else proxy.host
    port = proxy.get("port") if isinstance(proxy, dict) else proxy.port
    username = proxy.get("username") if isinstance(proxy, dict) else proxy.username
    password = proxy.get("password") if isinstance(proxy, dict) else proxy.password

    auth = ""
    if username:
        auth = username
        if password:
            auth += f":{'******' if mask_password else password}"
        auth += "@"
    return f"{proxy_type}://{auth}{host}:{port}"


def serialize_proxy(proxy: AccountProxy | None) -> dict | None:
    if not proxy:
        return None
    return {
        "account": proxy.account_name,
        "enabled": bool(proxy.enabled),
        "proxy_type": proxy.proxy_type,
        "host": proxy.host,
        "port": proxy.port,
        "username": proxy.username,
        "proxy_url": format_proxy_url(proxy, mask_password=False),
        "proxy_url_masked": format_proxy_url(proxy, mask_password=True),
        "updated_at": proxy.updated_at.isoformat() if proxy.updated_at else None,
    }


def get_proxy_binding(db: Session, account: str) -> AccountProxy | None:
    return db.query(AccountProxy).filter(AccountProxy.account_name == account).first()


def get_default_proxy_source(db: Session) -> str:
    row = db.query(SystemKV).filter(SystemKV.k == "default_proxy_source").first()
    value = (row.v if row and row.v is not None else DEFAULT_PROXY_SOURCE) or DEFAULT_PROXY_SOURCE
    return _clean_raw_proxy_value(value) or DEFAULT_PROXY_SOURCE


def set_default_proxy_source(db: Session, source_value: str | None) -> str:
    normalized = _clean_raw_proxy_value(source_value) or DEFAULT_PROXY_SOURCE
    row = db.query(SystemKV).filter(SystemKV.k == "default_proxy_source").first()
    if row:
        row.v = normalized
    else:
        row = SystemKV(k="default_proxy_source", v=normalized)
        db.add(row)
    db.commit()
    return normalized


def delete_proxy_binding(db: Session, account: str) -> bool:
    row = get_proxy_binding(db, account)
    if not row:
        return False
    db.delete(row)
    db.commit()
    return True


def list_proxy_bindings(db: Session, accounts: list[str] | None = None) -> dict[str, dict]:
    q = db.query(AccountProxy)
    if accounts:
        q = q.filter(AccountProxy.account_name.in_(accounts))
    rows = q.all()
    return {row.account_name: serialize_proxy(row) for row in rows}


def upsert_proxy_binding(db: Session, account: str, proxy_input: str | None, enabled: bool = True) -> dict | None:
    account_name = (account or "").strip()
    if not account_name:
        raise ValueError("missing_account")

    existing = get_proxy_binding(db, account_name)
    value = (proxy_input or "").strip()
    if not value:
        if existing:
            db.delete(existing)
            db.commit()
        return None

    parsed = parse_proxy_input(value)
    if parsed is None:
        return None

    if existing is None:
        existing = AccountProxy(account_name=account_name)
        db.add(existing)

    existing.enabled = 1 if enabled else 0
    existing.proxy_type = parsed["proxy_type"]
    existing.host = parsed["host"]
    existing.port = parsed["port"]
    existing.username = parsed["username"]
    existing.password = parsed["password"]
    db.commit()
    db.refresh(existing)
    return serialize_proxy(existing)
