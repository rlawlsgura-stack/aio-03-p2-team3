import { useEffect, useMemo, useRef } from "react"
import { Link } from "react-router-dom"

const LABELS = {
  product_family: "상품군", document_number: "문서번호", currency: "통화", incoterms: "납품 조건",
  payment_terms: "지급 조건", ship_to: "배송지", bill_to: "청구지", country_of_origin: "원산지",
  shipment_reference: "선적 참조", container_count: "컨테이너 수", actual_package_count: "포장 수",
  declared_net_weight_kg: "원본 순중량 (kg)", declared_gross_weight_kg: "원본 총중량 (kg)",
  declared_volume_cbm: "원본 부피 (CBM)", declared_total_quantity: "원본 총수량",
  declared_total_quantity_unit: "원본 수량 단위", legal_name: "회사명 (영문)", address: "주소 (영문)",
  remarks: "비고", package_type: "포장 종류", package_count: "포장 수량",
  net_weight_kg: "순중량 (kg)", gross_weight_kg: "총중량 (kg)", volume_cbm: "부피 (CBM)",
  dimensions: "포장 규격", shipping_marks: "포장 표시",
  contact: "담당자", phone: "전화", email: "이메일", product_name: "품목명 (영문)",
  product_code: "상품 코드", capacity: "용량·규격", quantity: "수량", unit: "단위",
  unit_price: "단가", discount_percent: "할인율 (%)", free_quantity: "무상 수량", price_basis: "가격 기준",
}

function readPath(value, path) {
  return path.split(".").reduce((current, key) => current?.[key], value)
}

function validPath(value, path) {
  const parts = path.split(".")
  const last = parts.pop()
  const parent = parts.length ? readPath(value, parts.join(".")) : value
  return Boolean(parent && typeof parent === "object" && (last in parent || LABELS[last]))
}

export function updateMissingField(value, path, nextValue) {
  const parts = path.split(".")
  const copy = structuredClone(value)
  let target = copy
  for (const key of parts.slice(0, -1)) target = target[key]
  target[parts.at(-1)] = nextValue
  const itemIndex = parts[0] === "items" ? Number(parts[1]) : -1
  if (itemIndex >= 0 && parts.at(-1) === "quantity") target.paid_quantity = nextValue
  if (itemIndex >= 0 && ["product_name", "product_code", "capacity", "unit"].includes(parts.at(-1))) {
    target.confirm_price_override = false
    target.master_price = false
  }
  if (itemIndex >= 0 && parts.at(-1) === "unit_price") {
    target.price_source = "User-entered value in document review"
    target.master_price = false
  }
  return copy
}

function packingIssuePaths(issues) {
  const paths = []
  for (const raw of issues ?? []) {
    const message = String(typeof raw === "string" ? raw : raw?.message ?? "").replace(/^Packing:\s*/i, "")
    let match = /^Packing row (\d+): quantity and unit must be confirmed$/i.exec(message)
    if (match) paths.push(`items.${Number(match[1]) - 1}.quantity`, `items.${Number(match[1]) - 1}.unit`)
    match = /^Packing row (\d+): net weight exceeds gross weight$/i.exec(message)
    if (match) paths.push(`items.${Number(match[1]) - 1}.net_weight_kg`, `items.${Number(match[1]) - 1}.gross_weight_kg`)
    match = /^Packing row (\d+): (package count|net weight|gross weight|CBM) is invalid$/i.exec(message)
    if (match) {
      const field = { "package count": "package_count", "net weight": "net_weight_kg",
        "gross weight": "gross_weight_kg", cbm: "volume_cbm" }[match[2].toLowerCase()]
      paths.push(`items.${Number(match[1]) - 1}.${field}`)
    }
    match = /^Source (package count|net weight \(kg\)|gross weight \(kg\)|volume \(CBM\)|quantity) total is invalid$/i.exec(message)
    if (match) {
      const field = { "package count": "actual_package_count", "net weight (kg)": "declared_net_weight_kg",
        "gross weight (kg)": "declared_gross_weight_kg", "volume (cbm)": "declared_volume_cbm",
        quantity: "declared_total_quantity" }[match[1].toLowerCase()]
      paths.push(field)
    }
    if (/^Container count must be a non-negative whole number$/i.test(message)) paths.push("container_count")
    if (/^Source total quantity has conflicting units/i.test(message)) paths.push("declared_total_quantity_unit")
  }
  return paths
}

function issueText(issue) {
  return String(typeof issue === "string" ? issue : issue?.message ?? "").trim()
}

function itemIndex(text, document) {
  const numbered = /\bitem\s+(\d+)\b/i.exec(text)
  if (numbered) return Number(numbered[1]) - 1
  return (document.items ?? []).findIndex((item) => item.product_name &&
    text.toLowerCase().includes(item.product_name.toLowerCase()))
}

function availablePaths(document, paths) {
  return [...new Set(paths)].filter((path) => validPath(document, path))
}

export function fieldsForIssue(issue, document) {
  const text = issueText(issue).replace(/^Confirm missing information:\s*/i, "")
  if (/^(buyer|seller)\.[a-z_]+$|^(items|packages)\.\d+\.[a-z_]+$|^[a-z_]+$/.test(text)) {
    return availablePaths(document, [text])
  }
  const exact = /Exact catalog product match for (.+?)(?:;|$)/i.exec(text)
  const suggested = /Double-check this product: (.+?)(?: may be| has no unique|\.)/i.exec(text)
  const index = itemIndex(exact?.[1] ?? suggested?.[1] ?? text, document)
  if (/exact catalog product match|double-check this product/i.test(text) && index >= 0) {
    return availablePaths(document, ["items." + index + ".product_name", "items." + index + ".capacity", "items." + index + ".product_code"])
  }
  if (/Price source for |Cosmetics price for |catalog price|transaction-specific price/i.test(text) && index >= 0) {
    return availablePaths(document, ["items." + index + ".unit_price"])
  }
  const numeric = /Valid (?:positive )?number required for ([a-z ]+?)(?: for item (\d+))?$/i.exec(text)
  if (numeric) {
    const field = ({ "paid quantity": "quantity", quantity: "quantity", "unit price": "unit_price",
      "discount percent": "discount_percent", "free quantity": "free_quantity", "shipped quantity": "shipped_quantity",
      "container count": "container_count" })[numeric[1].toLowerCase()]
    if (field) {
      const row = numeric[2] ? Number(numeric[2]) - 1 : index
      return row >= 0 ? availablePaths(document, ["items." + row + "." + field]) : availablePaths(document, [field])
    }
  }
  const party = /^(Buyer|Seller) (legal name|address|contact|phone|email)/i.exec(text)
  if (party) return availablePaths(document, [party[1].toLowerCase() + "." + party[2].toLowerCase().replace(" ", "_")])
  if (/^(?:PO buyer must be the issuing company|PI seller must be the issuing company|CI seller must be the issuing company)/i.test(text)) {
    return availablePaths(document, [/^PO /.test(text) ? "buyer.legal_name" : "seller.legal_name"])
  }
  if (/PO seller must be the supplier/i.test(text)) return availablePaths(document, ["buyer.legal_name", "seller.legal_name"])
  const itemIssue = /^(Product name|Quantity unit|Price basis|Discount|Shipped quantity|Promotion|Price source).*?item (\d+)/i.exec(text)
  if (itemIssue) {
    const row = Number(itemIssue[2]) - 1
    const field = ({ "product name": "product_name", "quantity unit": "unit", "price basis": "price_basis",
      discount: "discount_percent", "shipped quantity": "quantity", promotion: "promotion", "price source": "unit_price" })[itemIssue[1].toLowerCase()]
    return availablePaths(document, field ? ["items." + row + "." + field] : [])
  }
  if (/Price basis .* does not match quantity unit/i.test(text) && index >= 0) {
    return availablePaths(document, ["items." + index + ".price_basis", "items." + index + ".unit"])
  }
  if (/^Price basis for /i.test(text) && index >= 0) return availablePaths(document, ["items." + index + ".price_basis"])
  if (/^Confirm discount percentage/i.test(text) && index >= 0) return availablePaths(document, ["items." + index + ".discount_percent"])
  if (/^Shipped quantity must equal paid plus free quantity/i.test(text) && index >= 0) {
    return availablePaths(document, ["items." + index + ".quantity", "items." + index + ".free_quantity"])
  }
  if (/^Promotion (?:quantity mismatch|rule)/i.test(text) && index >= 0) {
    return availablePaths(document, ["items." + index + ".promotion", "items." + index + ".quantity", "items." + index + ".free_quantity"])
  }
  const translated = /^(Capacity|Unit|Price basis|Promotion|Price source) for item (\d+) must be translated into English/i.exec(text)
  if (translated) {
    const field = ({ capacity: "capacity", unit: "unit", "price basis": "price_basis", promotion: "promotion", "price source": "unit_price" })[translated[1].toLowerCase()]
    return availablePaths(document, ["items." + (Number(translated[2]) - 1) + "." + field])
  }
  if (/Product family must be confirmed/i.test(text)) return availablePaths(document, ["product_family"])
  if (/Currency .* must be confirmed|Currency rounding rule/i.test(text)) return availablePaths(document, ["currency"])
  if (/Document number is required/i.test(text)) return availablePaths(document, ["document_number"])
  if (/Incoterms and named place/i.test(text)) return availablePaths(document, ["incoterms"])
  if (/^Payment terms$/i.test(text)) return availablePaths(document, ["payment_terms"])
  if (/^Shipment reference$/i.test(text)) return availablePaths(document, ["shipment_reference"])
  if (/^Country of origin must be confirmed$/i.test(text)) return availablePaths(document, ["country_of_origin"])
  if (/^Container count must/i.test(text)) return availablePaths(document, ["container_count"])
  if (/^Actual package count must/i.test(text)) return availablePaths(document, ["actual_package_count"])
  if (/^Sum of package counts must equal actual package count/i.test(text)) {
    return availablePaths(document, ["actual_package_count", ...(document.packages ?? []).map((_, row) => "packages." + row + ".package_count")])
  }
  const packageIssue = /^(?:Packing: )?(?:Package count for package|Net weight cannot exceed gross weight for package)\s+(\d+)/i.exec(text)
  if (packageIssue) {
    const row = Number(packageIssue[1]) - 1
    const fields = /Package count/i.test(text) ? ["package_count"] : ["net_weight_kg", "gross_weight_kg"]
    return availablePaths(document, fields.map((field) => "packages." + row + "." + field))
  }
  if (/^(?:Actual package count|Source (?:package count|net weight \(kg\)|gross weight \(kg\)|volume \(CBM\)|quantity) total is invalid)/i.test(text)) {
    const field = /net weight/i.test(text) ? "declared_net_weight_kg" : /gross weight/i.test(text) ? "declared_gross_weight_kg"
      : /volume/i.test(text) ? "declared_volume_cbm" : /quantity/i.test(text) ? "declared_total_quantity" : "actual_package_count"
    return availablePaths(document, [field])
  }
  if (/Source quantity total unit does not match/i.test(text)) return availablePaths(document, ["declared_total_quantity", "declared_total_quantity_unit"])
  if (/Actual package count and weights/i.test(text)) return availablePaths(document, ["actual_package_count", "declared_net_weight_kg", "declared_gross_weight_kg"])
  if (/At least one line item is required/i.test(text)) return availablePaths(document, ["items.0.product_name"])
  return availablePaths(document, packingIssuePaths([issue]))
}

function resolvedPaths(missing, issues, draft) {
  return [...new Set([...(missing ?? []), ...(issues ?? [])]
    .flatMap((issue) => fieldsForIssue(issue, draft)))]
}

function fieldLabel(path) {
  const [first, index, key] = path.split(".")
  if (first === "buyer" || first === "seller") return `${first === "buyer" ? "구매자" : "판매자"} · ${LABELS[index] ?? index}`
  if ((first === "items" || first === "packages") && key) return `${first === "items" ? "품목" : "포장"} ${Number(index) + 1} · ${LABELS[key] ?? key}`
  return LABELS[first] ?? first
}

export function DocumentIssueList({ issues = [], missing = [], document, disabled, onOpen }) {
  const entries = [...issues.map((issue) => ({ issue, text: issueText(issue) })),
    ...missing.map((issue) => ({ issue, text: issueText(issue) }))]
    .filter((entry, index, all) => entry.text && all.findIndex((candidate) => candidate.text === entry.text) === index)
  if (!entries.length) return null
  return <ul className="document-issues" role="alert">{entries.map(({ issue, text }) => {
    const fields = fieldsForIssue(issue, document)
    const label = fields.length === 1 ? fieldLabel(fields[0]) + " 수정" : "관련 항목 수정"
    return <li key={text}><button type="button" className="document-issue-action" disabled={disabled}
      onClick={() => onOpen(fields, text)}>{text}<span>{fields.length ? label : "입력 팝업 열기"}</span></button></li>
  })}</ul>
}

export default function MissingFieldsDialog({ open, kind, draft, missing, issues, fields, issue = "", busy, dirty, onChange, onSave, onClose }) {
  const ref = useRef(null)
  const paths = useMemo(() => fields ?? resolvedPaths(missing, issues, draft), [fields, missing, issues, draft])

  useEffect(() => {
    const dialog = ref.current
    if (open && dialog && !dialog.open) dialog.showModal()
    else if (!open && dialog?.open) dialog.close()
  }, [open])

  return <dialog ref={ref} className="missing-fields-dialog" aria-labelledby="missing-fields-title" onCancel={(event) => { event.preventDefault(); onClose() }} onClose={onClose}>
    <div className="missing-fields-head"><div><p className="eyebrow">{kind} document</p><h2 id="missing-fields-title">누락 정보를 입력해 주세요</h2>
      <p>값을 입력한 뒤 저장하면 문서의 새 버전으로 반영됩니다.</p></div>
      <button className="missing-fields-close" type="button" onClick={onClose} aria-label="팝업 닫기">×</button></div>
    {issue && <p className="document-issue-context">{issue}</p>}
    {paths.length ? <div className="missing-fields-form">
      {paths.map((path) => {
        const value = readPath(draft, path) ?? ""
        const label = fieldLabel(path)
        const numeric = /(?:quantity|price|percent|weight|volume|package_count|container_count|total_quantity)/.test(path)
        const multiline = /(?:address|remarks|ship_to|bill_to|terms)/.test(path)
        const Control = multiline ? "textarea" : "input"
        return <div className="field" key={path}><label htmlFor={`missing-${path.replaceAll(".", "-")}`}>{label}</label>
          <Control id={`missing-${path.replaceAll(".", "-")}`} value={value} maxLength={4000} disabled={busy} inputMode={numeric ? "decimal" : undefined}
            aria-invalid={!String(value).trim() || undefined} onChange={(event) => onChange(path, event.target.value)} />
          {path.endsWith(".unit_price") && <label className="missing-fields-confirm"><input type="checkbox" disabled={busy} checked={Boolean(readPath(draft, `${path.slice(0, path.lastIndexOf("."))}.confirm_price_override`))}
            onChange={(event) => onChange(`${path.slice(0, path.lastIndexOf("."))}.confirm_price_override`, event.target.checked)} /> 단가표 대신 입력한 단가 사용</label>}
        </div>
      })}
    </div> : <p className="missing-fields-empty">이 오류는 문서 입력값이 아니라 설정 정보 확인이 필요합니다. <Link to={`/settings${kind === "PI" ? "?section=bank" : ""}`} onClick={onClose}>설정 열기</Link></p>}
    <div className="missing-fields-actions"><button className="btn" type="button" disabled={busy} onClick={onClose}>닫기</button>
      <button className="btn blue" type="button" disabled={busy || !dirty || !paths.length} onClick={onSave}>{busy ? "저장 중…" : "저장하고 반영"}</button></div>
  </dialog>
}
