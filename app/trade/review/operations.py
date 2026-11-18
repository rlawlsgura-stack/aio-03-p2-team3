"""최종 검토의 내용·버전·선택을 확인한다. LLM과 메일 발송을 호출하지 않는다."""
from copy import deepcopy
from hashlib import sha256
import json
from typing import Callable

from app.trade.common.models import DocumentError
from app.trade.review.models import Branding, Decision, RenderedFile, ReviewRepository, ReviewRequest


def content_hash(payload: dict) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return sha256(canonical.encode("utf-8")).hexdigest()


def propose(document_id: int, request: ReviewRequest, *, repository: ReviewRepository,
            check_document: Callable[[dict], None]) -> dict:
    document = repository.document(document_id)
    if document["revision"] != request.revision:
        raise DocumentError("REVISION_CONFLICT", "최신 문서를 다시 확인하세요")
    if document["status"] != "ready" or document.get("blocking_issues"):
        raise DocumentError("DOCUMENT_NOT_READY", "빠진 항목과 오류를 먼저 확인하세요")
    check_document(document)
    branding = repository.branding()
    payload = {"document_id": document_id, "document_type": document["document_type"],
               "revision": request.revision, "document": deepcopy(document),
               "formats": sorted(set(request.formats)),
               "seal_sha256": sha256(branding.seal).hexdigest(),
               "logo_sha256": sha256(branding.logo).hexdigest() if branding.logo else None}
    return repository.create(document_id, request.revision, payload, content_hash(payload), branding)


def check_decision(proposal_id: str, document_id: int, decision: Decision, *, repository: ReviewRepository) -> dict:
    proposal = repository.get(proposal_id)
    if proposal["document_id"] != document_id:
        raise DocumentError("PROPOSAL_NOT_FOUND", "검토 제안을 찾을 수 없습니다", 404)
    if (proposal["revision"] != decision.revision or proposal["content_hash"] != decision.content_hash
            or content_hash(proposal["payload"]) != proposal["content_hash"]):
        raise DocumentError("PROPOSAL_MISMATCH", "확인한 내용과 검토 제안이 일치하지 않습니다")
    return proposal


def get_proposal(proposal_id: str, document_id: int, *, repository: ReviewRepository) -> dict:
    proposal = repository.get(proposal_id)
    if proposal["document_id"] != document_id:
        raise DocumentError("PROPOSAL_NOT_FOUND", "검토 제안을 찾을 수 없습니다", 404)
    return proposal


def _render_failure_message(error: Exception) -> str:
    detail = str(error)
    if detail.startswith("XLSX_TEXT_FIELD:"):
        field = detail.partition(":")[2]
        return f"XLSX 발급 > {field}: 한글 또는 지원되지 않는 문자가 있습니다. 해당 값을 영문으로 바꾸세요."
    if detail.startswith("PDF_IMAGE_FIELD:"):
        image = detail.partition(":")[2]
        return f"회사 설정 > 이미지 > {image}: PNG 파일이 손상됐거나 지원되지 않는 형식입니다. 해당 이미지를 PNG로 다시 등록하세요."
    if detail in {"A company seal is required to create a trade-document PDF",
                  "The company seal could not be embedded in the PDF"}:
        return "회사 설정 > 회사 이미지 > 직인: 유효한 PNG 직인을 등록한 뒤 새 검토를 만들고 승인하세요."
    if detail == "A product row is too tall to fit on one PDF page; shorten its description":
        return "문서 작성 > 품목 > 품목명 또는 용량·규격: 내용이 너무 길어 PDF 한 페이지에 들어가지 않습니다. 해당 값을 짧게 줄이세요."
    if detail == "Document content exceeds the reserved footer area; add a page break or shorten the source information":
        return "문서 작성 > 거래 조건 > 비고: 비고가 PDF 하단 공간을 넘습니다. 비고 내용을 짧게 줄이세요."
    if detail == "지원하지 않는 파일 형식":
        return "전체 확인 > 발급 형식: PDF 또는 XLSX를 선택하고 새 검토를 만들어 주세요."
    if isinstance(error, ArithmeticError):
        return "문서 작성 > 품목 > 유상 수량·무상 수량·단가·할인율: 숫자 값을 확인하고 다시 검토하세요."
    if isinstance(error, TypeError):
        return "문서 작성 > 품목 또는 거래 조건: 입력한 값의 형식을 확인하고 다시 검토하세요."
    return "파일을 생성하지 못했습니다. 입력값과 발급 형식을 확인한 뒤 다시 검토해 주세요."


def download_file(file_id: str, *, repository: ReviewRepository) -> dict:
    return repository.file(file_id)


def decide(proposal_id: str, document_id: int, decision: Decision, *, repository: ReviewRepository,
           renderer: Callable[[dict, Branding], list[RenderedFile]]) -> dict:
    proposal = check_decision(proposal_id, document_id, decision, repository=repository)
    if decision.action == "cancel":
        return repository.cancel(proposal_id, decision.revision, decision.content_hash)
    if decision.action != "approve":
        raise DocumentError("MODIFIED_DOCUMENT_REQUIRED", "수정할 문서 내용을 함께 보내세요", 422)
    if proposal["status"] == "approved":
        return proposal
    if proposal["status"] != "pending":
        raise DocumentError("PROPOSAL_CONFLICT", "현재 대기 중인 검토 제안을 다시 확인하세요")
    document = repository.document(document_id)
    if document["revision"] != decision.revision or document["status"] != "ready":
        raise DocumentError("REVISION_CONFLICT", "최신 문서를 다시 확인하세요")
    branding = repository.proposal_branding(proposal)
    if (sha256(branding.seal).hexdigest() != proposal["payload"]["seal_sha256"]
            or (sha256(branding.logo).hexdigest() if branding.logo else None) != proposal["payload"]["logo_sha256"]):
        raise DocumentError("PROPOSAL_MISMATCH", "검토한 회사 이미지와 저장된 이미지가 일치하지 않습니다")
    try:
        files = renderer(proposal["payload"], branding)
    except (ValueError, ArithmeticError, TypeError) as error:
        raise DocumentError("RENDER_FAILED", _render_failure_message(error), 422) from error
    if (sorted(file.file_format for file in files) != sorted(proposal["payload"]["formats"])
            or any(not file.content for file in files)):
        raise DocumentError("RENDER_FAILED", "전체 확인 > 발급 형식: 선택한 PDF/XLSX 중 일부가 생성되지 않았습니다. 발급 형식을 다시 선택하고 검토하세요.", 422)
    return repository.approve(proposal_id, decision.revision, decision.content_hash, files)
