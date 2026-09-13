import pytest

from judge.connectors.instatus import InstatusClient, ref_from_marker
from judge.connectors.transport import ConnectorError
from judge.core.outbox import marker
from judge.core.models import CustomerImpact
from judge.safety.templates import INSTATUS_COMPONENT_STATUS, public_message, public_title
from judge.settings import Config


async def test_create_update_find_by_ref_delete(settings, http, uid):
    client = InstatusClient(settings, http)
    cfg = Config()
    svc = [cfg.catalog["checkout"]]
    mk = marker(f"{uid}abcdef01", f"inc_{uid}", f"t{uid}")
    ref = ref_from_marker(mk)
    assert ref.startswith(f"IJ-t{uid}-")
    assert await client.find_incident_by_ref(ref) is None

    status = INSTATUS_COMPONENT_STATUS[CustomerImpact.major_outage]
    inc_id = await client.create_incident(public_title(CustomerImpact.major_outage, svc),
                                          public_message("investigating", svc, ref), ["comp_checkout"], status)
    assert await client.find_incident_by_ref(ref) == inc_id
    inc = await client.get_incident(inc_id)
    assert inc["status"] == "INVESTIGATING" and inc["published"] is False  # dev default shouldPublish=false
    assert inc["components"][0]["status"] == "MAJOROUTAGE"
    assert "IJ-KEY" not in inc["updates"][0]["message"]  # internal marker never public

    await client.add_update(inc_id, public_message("resolved", svc, ref), "RESOLVED", ["comp_checkout"],
                            "OPERATIONAL")
    inc = await client.get_incident(inc_id)
    assert inc["status"] == "RESOLVED" and inc["resolved"] and len(inc["updates"]) == 2
    assert inc["components"][0]["status"] == "OPERATIONAL"

    await client.delete_incident(inc_id)
    with pytest.raises(ConnectorError) as e:
        await client.get_incident(inc_id)
    assert e.value.status == 404


async def test_invalid_component_status_rejected(settings, http):
    with pytest.raises(ConnectorError) as e:
        await InstatusClient(settings, http).create_incident("x", "y", ["comp_search"], "BROKEN")
    assert e.value.status == 400


async def test_set_components_adds_component(settings, http):
    instatus_client = InstatusClient(settings, http)
    iid = await instatus_client.create_incident("Outage: Checkout & payments", "We are investigating. Ref IJ-t1-0123abcd",
                                                ["comp_checkout"], "MAJOROUTAGE")
    await instatus_client.set_components(iid, ["comp_checkout", "comp_search"], "PARTIALOUTAGE")
    inc = await instatus_client.get_incident(iid)
    assert {c["id"] for c in inc["components"]} == {"comp_checkout", "comp_search"}
