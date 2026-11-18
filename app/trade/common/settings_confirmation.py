"""설정과 다른 문서 입력은 현재 값에 묶인 사용자 확인으로만 유지한다."""
from decimal import Decimal, InvalidOperation
import re

from app.trade.common.counterparties import name_key
from app.trade.common.models import DocumentError


PARTY_FIELDS = ("legal_name", "address", "contact", "phone", "email", "registration_details")
ITEM_FIELDS = ("product_name", "product_code", "capacity", "unit", "unit_price", "price_basis")
LABELS = {"legal_name": "회사명 (영문)", "address": "주소 (영문)", "contact": "담당자",
          "phone": "전화번호", "email": "이메일", "registration_details": "사업자 정보",
          "product_name": "품목명", "product_code": "상품 코드", "capacity": "규격",
          "unit": "단위", "unit_price": "단가", "price_basis": "가격 기준"}


def current_value(data, path):
    """허용된 입력 경로만 읽는다. 빈 값도 가격 확인의 맥락에 포함한다."""
    parts = path.split(".")
    if path in {"currency", "ship_to"}:
        value = data.get(path)
    elif len(parts) == 2 and parts[0] in {"buyer", "seller"} and parts[1] in PARTY_FIELDS:
        value = (data.get(parts[0]) or {}).get(parts[1])
    elif (len(parts) == 3 and parts[0] == "items" and parts[1].isdigit()
          and parts[2] in ITEM_FIELDS and int(parts[1]) < len(data.get("items") or [])):
        value = data["items"][int(parts[1])].get(parts[2])
    else:
        return None
    text = str(value if value is not None else "").strip()
    if parts[-1] == "unit_price" and text:
        try:
            number = Decimal(text)
            if number.is_finite():
                return format(number.normalize(), "f")
        except InvalidOperation:
            pass
    return text


def confirmation_values(data):
    paths = ["currency", "ship_to"]
    paths.extend(f"{role}.{field}" for role in ("buyer", "seller") for field in PARTY_FIELDS)
    paths.extend(f"items.{index}.{field}" for index, _ in enumerate(data.get("items") or [])
                 for field in ITEM_FIELDS)
    return {path: current_value(data, path) for path in paths}


def is_confirmed(data, path):
    value = current_value(data, path)
    return bool(value) and (data.get("confirmed_settings") or {}).get(path) == value


def _paths_confirmed(data, paths):
    confirmed = data.get("confirmed_settings") or {}
    return all(path in confirmed and confirmed[path] == current_value(data, path) for path in paths)


def product_confirmed(data, index):
    return _paths_confirmed(data, [f"items.{index}.{field}" for field in ITEM_FIELDS
                                  if field not in {"unit_price", "price_basis"}])


def price_confirmed(data, index):
    role = "seller" if data.get("document_type") == "PO" else "buyer"
    paths = ["currency", f"{role}.legal_name", *(f"items.{index}.{field}" for field in ITEM_FIELDS)]
    return is_confirmed(data, f"items.{index}.unit_price") and _paths_confirmed(data, paths)


def confirm_settings_input(data, context, document_type, *, required=False):
    """수기 저장 전에는 확인을 요청하고, AI 추출은 미확인 항목을 남겨 보완하게 한다."""
    actual = confirmation_values(data)
    previous_confirmation = data.get("confirmed_settings") or {}
    data["confirmed_settings"] = {path: value for path, value in (data.get("confirmed_settings") or {}).items()
                                  if path in actual and value == actual[path]}
    for index, item in enumerate(data.get("items") or []):
        if (f"items.{index}.unit_price" in previous_confirmation
                and not price_confirmed({**data, "document_type": document_type}, index)):
            # A saved override checkbox is not a new approval of changed values.
            item["confirm_price_override"] = False
    warnings = []
    requested = dict(data["confirmed_settings"])

    def warn(path, expected=None, *, confirmed=None):
        value = actual.get(path)
        if not value or (is_confirmed(data, path) if confirmed is None else confirmed):
            return
        parts = path.split(".")
        label = (f"{'구매자' if parts[0] == 'buyer' else '판매자'} · {LABELS[parts[1]]}"
                 if parts[0] in {"buyer", "seller"} else
                 f"품목 {int(parts[1]) + 1} · {LABELS[parts[2]]}" if parts[0] == "items" else "배송지")
        reference = f"설정값: {expected}" if expected not in (None, "") else "설정에 일치하는 값이 없습니다"
        warnings.append((path, f"{label}: {value} ({reference})"))
        paths = [path]
        if parts[0] == "items":
            paths.extend(f"items.{parts[1]}.{field}" for field in ITEM_FIELDS
                         if field not in {"unit_price", "price_basis"})
            if actual.get(f"items.{parts[1]}.unit_price"):
                price_role = "seller" if document_type == "PO" else "buyer"
                paths.extend(["currency", f"{price_role}.legal_name",
                              *(f"items.{parts[1]}.{field}" for field in ITEM_FIELDS)])
        requested.update({key: actual[key] for key in paths})

    issuer_role = "buyer" if document_type == "PO" else "seller"
    issuer = context.issuer or {}
    issuer_name = next((issuer.get(key) for key in ("legal_name_en", "legal_name", "company_name")
                        if issuer.get(key)), None)
    defaults = {"legal_name": issuer_name, **{field: issuer.get(field) for field in PARTY_FIELDS[1:]}}
    for field, expected in defaults.items():
        path = f"{issuer_role}.{field}"
        if actual[path] and (expected or field == "legal_name") and actual[path] != str(expected or "").strip():
            warn(path, expected)

    role = "seller" if document_type == "PO" else "buyer"
    supplied_name = actual[f"{role}.legal_name"]
    profiles = context.counterparties or []
    if profiles and supplied_name:
        key = name_key(supplied_name)
        matches = [profile for profile in profiles if key and key in {
            name_key(value) for value in [profile.get("legal_name"), *(profile.get("aliases") or [])]}]
        if len(matches) != 1:
            warn(f"{role}.legal_name")
        else:
            profile = matches[0]
            for field in PARTY_FIELDS:
                path, expected = f"{role}.{field}", profile.get(field)
                if expected and actual[path] and actual[path] != str(expected).strip():
                    warn(path, expected)
            destination = profile.get("default_ship_to")
            if destination and profile.get("ship_to_phone"):
                destination += " / Tel: " + profile["ship_to_phone"]
            if document_type != "PO" and destination and actual["ship_to"] and actual["ship_to"] != destination:
                warn("ship_to", destination)

    # Imports stay local: pricing also uses the value-bound confirmation helpers.
    from app.trade.common.pricing import build_trade_catalog, match_catalog_product, resolve_price, _matching_party_key
    from app.trade.pi.samples import is_manual_sample
    items = [] if document_type == "PL" else data.get("items") or []
    catalog = build_trade_catalog(context.price_rows if items else [])
    price_table = "purchase_prices" if document_type == "PO" else "buyer_prices"
    party_field = "supplier_key" if document_type == "PO" else "buyer_key"
    counterparty_key = name_key(supplied_name)
    for index, item in enumerate(items):
        if is_manual_sample({**data, "document_type": document_type}, item) or not item.get("product_name"):
            continue
        match = match_catalog_product(item.get("product_name"), item.get("capacity"), catalog["products"], item.get("product_code"))
        if not catalog["products"] and document_type != "PO" and data.get("product_family") != "cosmetics":
            continue
        if match["status"] != "matched":
            warn(f"items.{index}.product_name", confirmed=product_confirmed(data, index))
            price = None
        else:
            product = match["product"]
            if item.get("product_name") != product["item_name"]:
                warn(f"items.{index}.product_name", product["item_name"], confirmed=product_confirmed(data, index))
            for field, expected in (("product_code", product.get("source_code")), ("capacity", product.get("capacity"))):
                if expected and item.get(field) and item[field] != expected:
                    warn(f"items.{index}.{field}", expected, confirmed=product_confirmed(data, index))
            rows = [row for row in catalog[price_table] if row.get("product_id") == product["id"]
                    and (not data.get("currency") or row.get("currency") == data["currency"])]
            party_key = _matching_party_key(rows, party_field, counterparty_key)
            price = resolve_price(product["id"], catalog, "purchase" if document_type == "PO" else "sale",
                                  data.get("currency"), party_key) if party_key is not None else None
            if price and price.get("price_basis"):
                from app.trade.common.trade_documents import normalize_quantity_unit
                basis = re.sub(r"^(?:USD|KRW|EUR|CNY)\s*(?:/\s*|PER\s+)?|^PER\s+", "",
                               str(price["price_basis"]).strip(), flags=re.I)
                for field in ("unit", "price_basis"):
                    entered = re.sub(r"^(?:USD|KRW|EUR|CNY)\s*(?:/\s*|PER\s+)?|^PER\s+", "",
                                     str(item.get(field) or "").strip(), flags=re.I)
                    if entered and normalize_quantity_unit(entered) != normalize_quantity_unit(basis):
                        warn(f"items.{index}.{field}", price["price_basis"])
        if document_type == "PL" or item.get("unit_price") in (None, "") or item.get("confirm_price_override") is True:
            continue
        try:
            differs = price is None or Decimal(str(item["unit_price"])) != Decimal(str(price["unit_price"]))
        except (InvalidOperation, TypeError, ValueError):
            differs = False  # The ordinary numeric validation still rejects this value.
        if differs:
            warn(f"items.{index}.unit_price", price.get("unit_price") if price else None,
                 confirmed=price_confirmed({**data, "document_type": document_type}, index))
    if warnings and required:
        message = "\n".join(message for _, message in warnings)
        raise DocumentError("SETTINGS_CONFIRMATION_REQUIRED",
            message + "\n\n설정과 다르거나 등록되지 않은 값을 입력한 그대로 이 문서에 사용하시겠습니까? "
            "설정 원본과 기존 발급 파일은 바뀌지 않으며, 필수값·숫자 형식은 계속 검증합니다.",
            409, details={"confirmed_settings": requested})
    return [f"Confirm settings value: {path}" for path, _ in warnings]


def complete_confirmed_defaults(document):
    for path, value in (document.get("confirmed_settings") or {}).items():
        if value == "" and current_value(document, path):
            # Empty inputs filled from settings become part of this saved context.
            document["confirmed_settings"][path] = current_value(document, path)


def restore_unconfirmed_values(document, original, issues):
    """미확인 추출값도 잃지 않고 보여 준다. 이 값으로 발급할 승인은 하지 않는다."""
    for issue in issues:
        path = issue.removeprefix("Confirm settings value: ")
        value = current_value(original, path)
        if not value:
            continue
        parts = path.split(".")
        if parts[0] == "items":
            item = document["items"][int(parts[1])]
            item[parts[2]] = value
            if parts[2] == "unit_price":
                item.update(master_price=False, price_override_confirmed=False,
                            price_source="Entered document value; confirmation required")
        elif len(parts) == 2:
            document[parts[0]][parts[1]] = value
        else:
            document[path] = value


def with_settings_issues(document, issues):
    complete_confirmed_defaults(document)
    if issues:
        pending = document.setdefault("missing_fields", [])
        pending.extend(issue for issue in issues if issue not in pending)
        document["blocking_issues"].extend(issue for issue in issues if issue not in document["blocking_issues"])
        document.update(status="needs_input", ready_for_issue=False)
    return document
