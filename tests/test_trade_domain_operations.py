"""새 도메인 업무는 HTTP·구체 DB 없이도 같은 결과와 승인 규칙을 사용한다."""
from copy import deepcopy

import pytest

from app.trade.ci import operations as ci
from app.trade.ci.models import CIInput, StandaloneCIInput
from app.trade.common.models import DocumentError
from app.trade.common.pdf import issuer_display_name
from app.trade.pi.models import PIInput
from app.trade.pi.operations import create_pi, revise_pi
from app.trade.pl import operations as pl
from app.trade.pl.models import PLInput, PackingDecision
from app.trade.po import operations as po
from app.trade.po.models import POInput
from tests.test_pi_domain import MemoryPIRepository, pi_input
from tests.test_trade_domain_api import pl_values, po_values


class MemoryDocuments(MemoryPIRepository):
    def replace(self, document_id, revision, document, review_reference=None):
        return super().replace(document_id, revision, document)


def test_po_direct_and_ai_facts_use_purchase_price_without_http_or_sql():
    def repository():
        repo = MemoryDocuments()
        repo.settings.price_rows.append({**repo.settings.price_rows[0], "id": 2, "role": "purchase", "unit_price_text": "3"})
        return repo
    command = POInput.model_validate(po_values())
    direct = po.create(command, repository=repository())
    extracted = po.extract("source", repository=repository(), extractor=lambda _: command.model_dump(mode="json"))
    assert direct.document == extracted.document and direct.document["total"] == "30.00"


def test_ci_uses_source_snapshot_even_when_context_has_a_different_price():
    source = create_pi(pi_input(), repository=MemoryDocuments()).document
    class Repository(MemoryDocuments):
        def source_pi(self, document_id, revision):
            assert (document_id, revision) == (10, 3)
            return deepcopy(source)
        def context(self, currency):
            raise AssertionError("CI must not select today's catalog or bank")
    command = CIInput(source_pi_id=10, source_revision=3, document_number="CI-TEST",
                      shipment_reference="SHIP", country_of_origin="Korea")
    direct = ci.create(command, repository=Repository())
    extracted = ci.extract("source", repository=Repository(), source_pi_id=10, source_revision=3,
                           extractor=lambda _: {"document_number": "CI-TEST", "shipment_reference": "SHIP", "country_of_origin": "Korea"})
    assert direct.document == extracted.document and direct.document["total"] == "80.00"
    assert direct.document["bank"] is None


def test_pl_keeps_source_totals_until_a_bound_explicit_decision():
    command = PLInput.model_validate(pl_values())
    repository = MemoryDocuments()
    direct = pl.create(command, repository=repository)
    extracted = pl.extract("source", repository=MemoryDocuments(), extractor=lambda _: command.model_dump(mode="json"))
    assert direct.document == extracted.document and direct.status == "needs_input"
    before = deepcopy(direct.document)
    proposal = pl.packing_review(1, repository=repository)
    approved = pl.decide_packing(1, PackingDecision(action="approve", revision=1, content_hash=proposal["content_hash"]), repository=repository)
    assert approved.status == "ready"
    assert approved.document["_packing_original_totals"]["declared_volume_cbm"] == before["declared_volume_cbm"]
    assert direct.document == before


@pytest.mark.parametrize("kind", ["pi", "po", "ci", "pl"])
def test_document_uses_different_company_only_after_confirming_exact_input(kind):
    repository = MemoryDocuments()
    if kind == "pl":
        model, create, values = PLInput, pl.create, pl_values()
    elif kind == "po":
        model, create, values = POInput, po.create, po_values()
        repository.settings.price_rows.append({**repository.settings.price_rows[0],
                                               "id": 2, "role": "purchase", "unit_price_text": "3"})
    else:
        model, create, values = PIInput, create_pi, pi_input().model_dump(mode="json")
        if kind == "ci":
            model, create = StandaloneCIInput, ci.create
            values.pop("payment_terms")
            values["items"][0].pop("is_sample")
            values.update(mode="standalone", shipment_reference="SHIP", country_of_origin="Korea")
    role = "buyer" if kind == "po" else "seller"
    recipient = "seller" if kind == "po" else "buyer"
    values[role] = {"legal_name": "Alternate Example Trading", "address": "99 Fiction Road, Seoul"}
    values[recipient] = {"legal_name": "Example Alias", "address": "88 Delivery Road, Seoul"}
    repository.settings.counterparties.append({"company_key": "example-counterparty",
        "legal_name": "Registered Example Trading", "aliases": ["Example Alias"], "address": "1 Directory Road, Seoul"})
    settings_before = deepcopy(repository.settings)

    with pytest.raises(DocumentError) as confirmation:
        create(model.model_validate(values), repository=repository)
    assert confirmation.value.code == "SETTINGS_CONFIRMATION_REQUIRED"
    assert confirmation.value.status_code == 409 and repository.documents == {}
    values["confirmed_settings"] = confirmation.value.details["confirmed_settings"]
    saved = create(model.model_validate(values), repository=repository)

    assert saved.document[role]["legal_name"] == "Alternate Example Trading"
    assert saved.document[role]["address"] == "99 Fiction Road, Seoul"
    assert saved.document[recipient]["legal_name"] == "Example Alias"
    assert saved.document[recipient]["address"] == "88 Delivery Road, Seoul"
    assert issuer_display_name(saved.document) == "Alternate Example Trading"
    assert saved.input["confirmed_settings"] == saved.document["confirmed_settings"]
    assert repository.settings == settings_before
    if kind == "po":
        po.check(saved.document)  # 최종 검토도 같은 사용자 확인을 인정해야 한다.


def test_confirmed_custom_product_and_price_keep_history_and_require_reconfirmation():
    repository = MemoryDocuments()
    original = create_pi(pi_input(), repository=repository)
    old_document = deepcopy(original.document)
    old_settings = deepcopy(repository.settings)
    values = deepcopy(original.input)
    values["seller"]["legal_name"] = "Alternate Example Trading"
    values["items"][0].update(product_name="Made to Order Serum", capacity="75ml", unit_price="9")

    with pytest.raises(DocumentError) as confirmation:
        revise_pi(original.document_id, 1, PIInput.model_validate(values), repository=repository)
    assert confirmation.value.code == "SETTINGS_CONFIRMATION_REQUIRED"
    assert repository.get(original.document_id) == old_document
    values["confirmed_settings"] = confirmation.value.details["confirmed_settings"]
    revised = revise_pi(original.document_id, 1, PIInput.model_validate(values), repository=repository)
    assert revised.revision == 2 and revised.status == "ready"
    assert revised.document["items"][0]["product_name"] == "Made to Order Serum"
    assert revised.document["items"][0]["unit_price"] == "9"
    assert revised.document["total"] == "90.00"
    assert original.document == old_document and repository.settings == old_settings

    for path, replacement in (("seller", "Another Example Trading"), ("price", "11"), ("currency", "EUR")):
        stale = deepcopy(revised.input)
        if path == "seller":
            stale["seller"]["legal_name"] = replacement
        elif path == "price":
            stale["items"][0]["unit_price"] = replacement
        else:
            stale["currency"] = replacement
        with pytest.raises(DocumentError) as reconfirmation:
            revise_pi(original.document_id, 2, PIInput.model_validate(stale), repository=repository)
        assert reconfirmation.value.code == "SETTINGS_CONFIRMATION_REQUIRED"
        assert repository.get(original.document_id)["revision"] == 2

    incomplete = PIInput.model_validate({**revised.input, "payment_terms": None})
    saved = revise_pi(original.document_id, 2, incomplete, repository=repository)
    assert saved.status == "needs_input" and "Payment terms" in saved.document["blocking_issues"]
