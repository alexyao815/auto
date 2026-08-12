from __future__ import annotations

import base64
import time

import pytest

from automation_center.salt import FakeSaltAdapter, HttpSaltAdapter, create_salt_adapter


class Response:
    def __init__(self, payload, status_code=200): self.payload=payload; self.status_code=status_code
    def json(self): return self.payload
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError(f"HTTP {self.status_code}")


def test_fake_adapter_full_contract(settings):
    adapter = FakeSaltAdapter()
    adapter.add_pending("n2")
    assert adapter.pending_keys() == ["demo-minion", "n2"]
    adapter.reject_key("n2")
    adapter.accept_key("demo-minion")
    assert "demo-minion" in adapter.accepted_keys()
    assert adapter.ping("demo-minion")
    assert adapter.node_info("demo-minion")["hostname"] == "demo-minion"
    adapter.transfer_package("demo-minion", "salt://x", "/tmp/x")
    adapter.prepare_workdir("demo-minion", "/tmp/x", "/tmp/work")
    jid = adapter.start_step("demo-minion", "shell", "scripts/fix.sh", "/tmp/work", "/tmp/o", "/tmp/e", "/tmp/x")
    assert adapter.job_result(jid, "demo-minion").state == "RUNNING"
    time.sleep(0.06)
    assert adapter.job_result(jid, "demo-minion").state == "SUCCESS"
    data, offset = adapter.read_file("demo-minion", "/tmp/o", 0)
    assert data and offset == len(data)
    assert "termination" in adapter.terminate_job("demo-minion", jid)
    adapter.cleanup_workdir("demo-minion", "/tmp")
    assert adapter.cleaned_workdirs == [("demo-minion", "/tmp")]
    assert adapter.job_result("missing", "demo-minion").state == "LOST"
    settings.salt_mode = "fake"
    assert isinstance(create_salt_adapter(settings), FakeSaltAdapter)


def test_http_adapter_contract(monkeypatch, settings):
    settings.salt_mode = "http"; settings.salt_api_credential = "pw"
    job_mode = {"value": "success"}
    submitted = {"command": ""}
    def post(url, data=None, headers=None, timeout=None):
        if url.endswith('/login'):
            return Response({"return": [{"token": "token", "expire": time.time()+600}]})
        client = data.get("client"); fun = data.get("fun"); target = data.get("tgt", "node1")
        if client == "wheel": return Response({"return": [{"data": {"return": {"minions_pre": ["pending"], "minions": ["node1"]}}}]})
        if client == "runner":
            if fun == "jobs.list_jobs": return Response({"return": [{"202601010000": {"Function": "cmd.run_all"}}]})
            if job_mode["value"] == "running": return Response({"return": [{}]})
            if job_mode["value"] == "lost": return Response({"return": [{"other": {"retcode": 0}}]})
            if job_mode["value"] == "scalar": return Response({"return": [{"node1": "ok"}]})
            return Response({"return": [{"node1": {"retcode": 0, "stdout": "ok", "stderr": ""}}]})
        if client == "local_async":
            submitted["command"] = str(data.get("arg"))
            return Response({"return": [{"jid": "202601010000"}]})
        if fun == "test.ping": value = True
        elif fun == "grains.item": value = {"host": "node1", "fqdn_ip4": ["192.0.2.1"]}
        elif fun == "cmd.run" and data.get("arg") == ["ps -eo comm="]: value = "nova-compute\n"
        elif fun == "service.get_all": value = ["nova-compute.service"]
        elif fun == "cmd.run": value = base64.b64encode(b"log").decode()
        elif fun == "cp.get_file": value = "/tmp/package"
        elif fun == "cmd.run_all": value = {"retcode": 0, "stdout": "", "stderr": ""}
        elif fun == "saltutil.kill_job": value = True
        else: value = True
        return Response({"return": [{target: value}]})
    monkeypatch.setattr("automation_center.salt.httpx.post", post)
    adapter = HttpSaltAdapter(settings)
    assert adapter.pending_keys() == ["pending"] and adapter.accepted_keys() == ["node1"]
    adapter.accept_key("pending"); adapter.reject_key("pending")
    assert adapter.ping("node1")
    assert adapter.node_info("node1")["management_ip"] == "192.0.2.1"
    assert adapter.node_info("node1")["services"] == ["nova-compute.service"]
    adapter.transfer_package("node1", "salt://x", "/tmp/x")
    adapter.prepare_workdir("node1", "/tmp/x", "/tmp/work")
    jid = adapter.start_step("node1", "shell", "x.sh", "/tmp/work", "/tmp/o", "/tmp/e", "/tmp/x")
    assert jid == "202601010000"
    assert "mkdir -p /tmp" in submitted["command"]
    assert adapter.job_result(jid, "node1").state == "SUCCESS"
    job_mode["value"] = "running"; assert adapter.job_result(jid, "node1").state == "RUNNING"
    job_mode["value"] = "lost"; assert adapter.job_result(jid, "node1").state == "LOST"
    job_mode["value"] = "scalar"; assert adapter.job_result(jid, "node1").stdout == "ok"
    data, offset = adapter.read_file("node1", "/tmp/o", 0); assert data == b"log" and offset == 3
    assert adapter.terminate_job("node1", jid) == "True"
    adapter.cleanup_workdir("node1", "/tmp/work")
    assert isinstance(create_salt_adapter(settings), HttpSaltAdapter)
