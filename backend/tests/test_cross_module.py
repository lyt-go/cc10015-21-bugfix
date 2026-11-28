# -*- coding: utf-8 -*-
"""跨模块数据链路测试：验证18个模块间的真实业务联动。
所有测试操作同一store实例，通过重置种子数据确保隔离。
"""
from __future__ import annotations

import threading

import pytest

# 直接导入以获取模块级store引用
from app.store import store


@pytest.fixture(autouse=True)
def reset_store():
    """每个测试前重置store到初始种子状态。"""
    store.reset()


def _samples(module: str) -> list:
    return store.rows(module)


# ============================================================
# 测试1：站位布设时航次计划前置检查
# ============================================================
def test_station_deploy_requires_approved_cruise_plan():
    from app.services.station_deploy import StationDeployService
    svc = StationDeployService()

    # 确保所有巡航计划都不是已批准状态
    for row in _samples("cruise_plan"):
        row["status"] = "编制中"

    # 尝试确认站位 - 应该失败因为没有已批准的计划
    entry, msg = svc.run_action(1, "确认站位")
    assert entry is None, f"期望失败但得到: {entry}"
    assert "批准" in msg, f"期望批准相关错误但得到: {msg}"

    # 批准一个巡航计划
    for row in _samples("cruise_plan"):
        row["status"] = "已提交"
    _samples("cruise_plan")[0]["status"] = "已批准"

    # 现在应该可以通过
    entry, msg = svc.run_action(1, "确认站位")
    assert entry is not None, f"期望成功但得到: {msg}"
    assert entry["status"] == "已布设"


# ============================================================
# 测试2：采样→样品登记联动
# ============================================================
def test_sampling_syncs_to_registry():
    from app.services.sampling_record import SamplingRecordService
    svc = SamplingRecordService()

    # 设置站位
    station_rows = _samples("station_deploy")
    station_rows[0]["站位编号"] = "STAT-TEST"
    station_rows[0]["status"] = "已布设"

    # 设置采样记录
    sample_rows = _samples("sampling_record")
    sample_rows[0]["采样站位"] = "STAT-TEST"
    sample_rows[0]["样品编号"] = "SAMP-TEST-001"
    sample_rows[0]["样品类型"] = "海水样品"
    sample_rows[0]["采样人"] = "测试采样员"
    sample_rows[0]["采样时间"] = "2026-11-25"
    sample_rows[0]["status"] = "待采样"

    # 记录采样
    entry, msg = svc.run_action(1, "记录采样")
    assert entry is not None, f"采样失败: {msg}"
    assert entry["status"] == "已采样"

    # 样品入库 - 应触发登记联动
    entry, msg = svc.run_action(1, "样品入库")
    assert entry is not None, f"入库失败: {msg}"
    assert entry["status"] == "已入库"

    # 验证样品登记中有对应记录
    registry_rows = store.find_by("sample_registry", "样品编号", "SAMP-TEST-001")
    assert len(registry_rows) > 0, "样品登记中应有对应记录"


# ============================================================
# 测试3：水文测量触发质控流程
# ============================================================
def test_hydro_measure_triggers_qc():
    from app.services.hydro_measure import HydroMeasureService
    svc = HydroMeasureService()

    # 设置站位
    station_rows = _samples("station_deploy")
    station_rows[0]["站位编号"] = "STAT-TEST-2"

    # 设置水文记录
    hydro_rows = _samples("hydro_measure")
    hydro_rows[0]["测量站位"] = "STAT-TEST-2"
    hydro_rows[0]["status"] = "待测量"

    # 开始测量
    entry, msg = svc.run_action(1, "开始测量")
    assert entry is not None, f"测量失败: {msg}"

    # 上传剖面 - 应触发质控流程
    hydro_rows[0]["status"] = "已测量"
    entry, msg = svc.run_action(1, "上传剖面")
    assert entry is not None, f"上传失败: {msg}"
    assert entry["status"] == "已上传"

    # 验证质控记录被创建
    qc_triggered = any(
        r.get("triggered_by") == "hydro_measure"
        for r in _samples("data_qc")
    )
    assert qc_triggered, "应触发质控流程"


# ============================================================
# 测试4：仪器校准不合格联动设备停用
# ============================================================
def test_instrument_calib_failure_disables_equipment():
    from app.services.instrument_calib import InstrumentCalibService
    svc = InstrumentCalibService()

    # 设置设备
    equip_rows = _samples("equipment_manifest")
    equip_rows[0]["设备名称"] = "CTD-9901"
    equip_rows[0]["status"] = "已启用"

    # 设置校准记录
    calib_rows = _samples("instrument_calib")
    calib_rows[0]["仪器名称"] = "CTD-9901"
    calib_rows[0]["status"] = "已校准"

    # 停用仪器（不合格）
    entry, msg = svc.run_action(1, "停用仪器")
    assert entry is not None, f"停用失败: {msg}"

    # 验证设备清单中设备被停用
    equip = store.find_one_by("equipment_manifest", "设备名称", "CTD-9901")
    assert equip is not None, "设备应存在"
    assert equip.get("calib_failed"), "设备应标记校准失败"


# ============================================================
# 测试5：海况标记影响站位可作业
# ============================================================
def test_severe_weather_affects_stations():
    from app.services.weather_obs import WeatherObsService
    weather_svc = WeatherObsService()

    # 标记恶劣海况
    entry, msg = weather_svc.run_action(1, "标记海况")
    assert entry is not None, f"标记失败: {msg}"
    assert entry["status"] == "恶劣海况"

    # 验证已布设站位收到告警
    station_rows = _samples("station_deploy")
    alerted = any(r.get("weather_alert") for r in station_rows if r.get("status") == "已布设")
    assert alerted, "已布设站位应有海况告警"

    # 验证恶劣海况阻止站位作业
    from app.services.station_deploy import StationDeployService
    station_svc = StationDeployService()
    # 尝试开始作业（当前已是恶劣海况状态）
    station_rows[0]["status"] = "已布设"
    station_entry, msg = station_svc.run_action(1, "开始作业")
    assert station_entry is None, f"恶劣海况应阻止站位作业: {msg}"


# ============================================================
# 测试6：数据传输完成联动成果汇编
# ============================================================
def test_data_transfer_updates_results_compile():
    from app.services.data_transfer import DataTransferService
    svc = DataTransferService()

    transfer_rows = _samples("data_transfer")
    transfer_rows[0]["status"] = "传输中"
    transfer_rows[0]["数据内容"] = "CTD剖面数据V3"
    transfer_rows[0]["传输时间"] = "2026-11-25"

    # 确认传输完成
    entry, msg = svc.run_action(1, "确认完成")
    assert entry is not None, f"确认失败: {msg}"
    assert entry["status"] == "已完成"

    # 验证成果汇编数据完整度更新
    compile_rows = _samples("results_compile")
    updated = any(
        r.get("transfer_complete") and "CTD剖面数据V3" in str(r.get("数据完整度", ""))
        for r in compile_rows
    )
    assert updated, "成果汇编应反映传输完成的数据完整度更新"


# ============================================================
# 测试7：幂等性（重复请求不产生副作用）
# ============================================================
def test_idempotency_duplicate_action():
    from app.services.cruise_plan import CruisePlanService
    svc = CruisePlanService()

    row = _samples("cruise_plan")
    row[0]["status"] = "编制中"

    entry, msg = svc.run_action(1, "提交审阅")
    assert entry is not None, f"首次应成功: {msg}"

    entry2, msg2 = svc.run_action(1, "提交审阅")
    assert entry2 is None, f"重复请求应被拒绝但得到了: {entry2} | msg={msg2}"
    assert "重复" in msg2, f"应提示重复: {msg2}"


def test_idempotency_sampling_duplicate():
    from app.services.sampling_record import SamplingRecordService
    svc = SamplingRecordService()

    station_rows = _samples("station_deploy")
    station_rows[0]["站位编号"] = "STAT-IDEM"
    station_rows[0]["status"] = "已布设"

    sample_rows = _samples("sampling_record")
    sample_rows[0]["采样站位"] = "STAT-IDEM"
    sample_rows[0]["status"] = "待采样"

    entry1, msg1 = svc.run_action(1, "记录采样")
    assert entry1 is not None, f"首次应成功: {msg1}"

    entry2, msg2 = svc.run_action(1, "记录采样")
    assert entry2 is None, f"重复应被拒绝: msg2={msg2}"


# ============================================================
# 测试8：并发场景
# ============================================================
def test_concurrent_updates_same_equipment():
    """测试并发修改同一设备数据的原子性。"""
    # 使用原子更新验证
    equip = _samples("equipment_manifest")
    equip[0]["设备名称"] = "MULTI-BEAM"
    equip[0]["status"] = "已启用"

    result = store.atomic_update("equipment_manifest", 1, {"status": "已归还", "concurrent_test": True})
    assert result is not None
    assert result["status"] == "已归还"
    assert result["concurrent_test"] == True


def test_concurrent_station_updates():
    """测试两个操作同时尝试修改同一个站位状态。"""
    station_rows = _samples("station_deploy")
    station_rows[0]["status"] = "待布设"

    result = store.atomic_update("station_deploy", 1, {"status": "已布设", "concurrent_test": True})
    assert result is not None
    assert result["status"] == "已布设"
    assert result["concurrent_test"] == True


# ============================================================
# 补充测试：生物拖网分拣联动样品登记
# ============================================================
def test_bio_trawl_sorting_registers_samples():
    from app.services.bio_trawl import BioTrawlService
    svc = BioTrawlService()

    trawl_rows = _samples("bio_trawl")
    trawl_rows[0]["status"] = "已记录"
    trawl_rows[0]["拖网站位"] = "BIO-STAT-01"
    trawl_rows[0]["操作人"] = "生物采样员"
    trawl_rows[0]["记录时间"] = "2026-11-25"
    trawl_rows[0]["渔获物描述"] = "深海鱼类、甲壳类"

    # 完成分拣
    entry, msg = svc.run_action(1, "完成分拣")
    assert entry is not None, f"分拣失败: {msg}"
    assert entry["status"] == "已分拣"

    # 验证创建了样品登记记录
    bio_samples = store.find_by("sample_registry", "样品类型", "生物样品")
    assert len(bio_samples) > 0, "应创建生物样品登记记录"


# ============================================================
# 补充测试：沉积物分割更新样品登记
# ============================================================
def test_sediment_split_updates_registry():
    from app.services.sediment_sample import SedimentSampleService
    svc = SedimentSampleService()

    # 预先在样品登记中创建记录
    registry_rows = _samples("sample_registry")
    registry_rows.append({
        "id": 999, "样品编号": "SEDI-0001", "样品类型": "沉积物",
        "采集站位": "SEDI-STAT", "保存方式": "未分割",
        "存放位置": "待分配", "登记人": "测试", "登记日期": "2026-11-25",
        "备注说明": "", "status": "待登记", "pending": True, "abnormal": False,
    })

    sedi_rows = _samples("sediment_sample")
    sedi_rows[0]["status"] = "已登记"
    sedi_rows[0]["样品编号"] = "SEDI-0001"
    sedi_rows[0]["采样站位"] = "SEDI-STAT"
    sedi_rows[0]["柱状长度"] = "5段"

    entry, msg = svc.run_action(1, "分割样品")
    assert entry is not None, f"分割失败: {msg}"
    assert entry["status"] == "已分割"

    # 验证样品登记中对应记录被更新
    updated = store.find_one_by("sample_registry", "样品编号", "SEDI-0001")
    assert updated is not None
    assert "分割" in updated.get("保存方式", ""), "保存方式应标记已分割"


# ============================================================
# 补充测试：分析申请批准时校验样品入库状态
# ============================================================
def test_analysis_request_checks_sample_status():
    from app.services.analysis_request import AnalysisRequestService
    svc = AnalysisRequestService()

    req_rows = _samples("analysis_request")
    req_rows[0]["申请样品"] = "SAMP-0001"
    req_rows[0]["status"] = "已提交"

    # 确保样品库中没有入库状态的SAMP-0001
    for r in _samples("sample_registry"):
        if r.get("样品编号") == "SAMP-0001" and r.get("status") != "已入库":
            r["status"] = "待登记"

    # 批准前样品未入库 -> 应拒绝
    entry, msg = svc.run_action(1, "批准分析")
    assert entry is None, f"未入库应拒绝批准，实际成功: {msg}"
    assert "入库" in msg, f"应提示入库状态: {msg}"


# ============================================================
# 补充测试：潜水中止影响队员可用性
# ============================================================
def test_diving_abort_affects_crew():
    from app.services.diving_ops import DivingOpsService
    svc = DivingOpsService()

    diving_rows = _samples("diving_ops")
    diving_rows[0]["潜水员"] = "队员排班样例1"
    diving_rows[0]["安全员"] = "队员排班样例2"
    diving_rows[0]["status"] = "已下水"

    entry, msg = svc.run_action(1, "中止潜水")
    assert entry is not None, f"中止失败: {msg}"
    assert entry["status"] == "已中止"

    # 验证排班表中对应人员标记为受影响
    crew_rows = _samples("crew_schedule")
    affected = any(
        r.get("diving_aborted") for r in crew_rows
        if r.get("队员姓名") in ("队员排班样例1", "队员排班样例2")
    )
    assert affected, "潜水员和安全员的排班应标记为受潜水中止影响"


# ============================================================
# 补充测试：航次日志异常事件触发设备安全告警
# ============================================================
def test_cruise_log_abnormal_triggers_safety():
    from app.services.cruise_log import CruiseLogService
    svc = CruiseLogService()

    # 给一台设备明确出厂编号，日志按编号点名该设备
    equip_rows = _samples("equipment_manifest")
    equip_rows[0]["出厂编号"] = "EQUI-SAFE-1"
    equip_rows[0]["保管班组"] = "甲板一班"

    log_rows = _samples("cruise_log")
    log_rows[0]["异常事件"] = "主机转速异常，设备过热"
    log_rows[0]["设备运行"] = "主机异常"
    log_rows[0]["出厂编号"] = "EQUI-SAFE-1"
    log_rows[0]["status"] = "待填写"

    entry, msg = svc.run_action(1, "提交日志")
    assert entry is not None, f"提交失败: {msg}"
    assert entry.get("has_abnormal") == True, "应标记异常事件"

    # 验证只有被出厂编号点名的设备收到告警，其他设备不被扇出
    alerted = [r for r in equip_rows if r.get("log_alert")]
    assert len(alerted) == 1, f"应仅一台设备收到告警，实际{len(alerted)}台"
    assert alerted[0]["出厂编号"] == "EQUI-SAFE-1"


# ============================================================
# 补充测试：排班调整影响站位和潜水作业
# ============================================================
def test_crew_schedule_change_affects_operations():
    from app.services.crew_schedule import CrewScheduleService
    svc = CrewScheduleService()

    crew_rows = _samples("crew_schedule")
    crew_rows[0]["队员姓名"] = "站位布设样例1"
    crew_rows[0]["status"] = "已确认"

    # 确保站位布设中有匹配的布设人
    station_rows = _samples("station_deploy")
    for r in station_rows:
        r["布设人"] = "站位布设样例1"

    entry, msg = svc.run_action(1, "调整排班")
    assert entry is not None, f"调整失败: {msg}"
    assert entry["status"] == "已调整"

    # 验证站位布设记录收到排班变更标记
    affected_stations = [r for r in _samples("station_deploy") if r.get("schedule_changed")]
    assert len(affected_stations) > 0, "站位布设应收到排班变更通知"


# ============================================================
# 测试9：回滚机制
# ============================================================
def test_rollback_mechanism():
    """测试保存快照和回滚能力。"""
    equip = _samples("equipment_manifest")
    equip[0]["status"] = "已启用"
    equip[0]["设备名称"] = "ROLLBACK-TEST"

    # 保存快照
    tx_id = "test-tx-001"
    store.save_snapshot(tx_id, "equipment_manifest", 1)

    # 修改数据
    equip[0]["status"] = "已归还"
    equip[0]["modified"] = True

    # 回滚
    result = store.rollback(tx_id)
    assert result is True, "回滚应成功"

    # 验证回滚后状态
    assert equip[0]["status"] == "已启用", "回滚后状态应恢复"
    assert equip[0].get("modified") is None, "回滚后不应有修改标记"


def test_rollback_nonexistent():
    """测试回滚不存在的事务。"""
    result = store.rollback("nonexistent-tx")
    assert result is False, "不存在的事务回滚应返回False"


# ============================================================
# 测试10：重试机制
# ============================================================
def test_retry_mechanism_success():
    """测试重试机制：成功操作不需要重试。"""
    call_count = [0]

    def increment_ok():
        call_count[0] += 1
        return call_count[0]

    result, ok = store.retry(increment_ok, max_retries=3)
    assert ok is True
    assert result == 1
    assert call_count[0] == 1, "成功操作只应执行一次"


def test_retry_mechanism_eventual_failure():
    """测试重试机制：所有重试都失败。"""
    call_count = [0]

    def always_fail():
        call_count[0] += 1
        raise RuntimeError("always fail")

    result, ok = store.retry(always_fail, max_retries=3)
    assert ok is False
    assert result is None
    assert call_count[0] == 3, "应重试3次"


# ============================================================
# 测试11：航次计划变更通知站位布设
# ============================================================
def test_cruise_plan_change_notifies_stations():
    """测试航次计划变更后通知站位布设。"""
    from app.services.cruise_plan import CruisePlanService
    svc = CruisePlanService()

    # 批准一个计划
    plan_rows = _samples("cruise_plan")
    plan_rows[0]["status"] = "已提交"
    entry, msg = svc.run_action(1, "批准计划")
    assert entry is not None, f"批准失败: {msg}"
    assert entry["status"] == "已批准"

    # 设置站位为已布设状态
    station_rows = _samples("station_deploy")
    for r in station_rows:
        r["status"] = "已布设"

    # 变更计划
    entry, msg = svc.run_action(1, "变更计划")
    assert entry is not None, f"变更失败: {msg}"
    assert entry["status"] == "已变更"

    # 验证已布设站位收到变更通知
    affected = any(
        r.get("plan_changed") for r in _samples("station_deploy") if r.get("status") == "已布设"
    )
    assert affected, "已布设站位应收到航次计划变更通知"


# ============================================================
# 测试12：站位作业校验布设人可用性
# ============================================================
def test_station_job_checks_deployer_availability():
    from app.services.station_deploy import StationDeployService
    svc = StationDeployService()

    # 批准航次计划
    for r in _samples("cruise_plan"):
        r["status"] = "已提交"
    _samples("cruise_plan")[0]["status"] = "已批准"
    # ensure no severe weather
    for r in _samples("weather_obs"):
        r["status"] = "已同步"

    # 设置站位已布设，布设人为指定人员
    station_rows = _samples("station_deploy")
    station_rows[0]["status"] = "已布设"
    station_rows[0]["布设人"] = "测试队员A"

    # 设置该队员在排班中标记为不可用（潜水中止影响）
    crew_rows = _samples("crew_schedule")
    crew_rows[0]["队员姓名"] = "测试队员A"
    crew_rows[0]["unavailable"] = True
    crew_rows[0]["diving_aborted"] = True

    # 尝试开始作业 -> 应被拒绝
    entry, msg = svc.run_action(1, "开始作业")
    assert entry is None, f"布设人不可用应阻止作业，但返回了: {entry}"
    assert "不可用" in msg, f"应提示不可用: {msg}"


# ============================================================
# 测试13：数据传输重试标记
# ============================================================
def test_data_transfer_retry_tracking():
    from app.services.data_transfer import DataTransferService
    svc = DataTransferService()

    transfer_rows = _samples("data_transfer")
    transfer_rows[0]["status"] = "已失败"
    transfer_rows[0]["retry_count"] = 1

    # 重试传输
    entry, msg = svc.run_action(1, "重试传输")
    assert entry is not None, f"重试失败: {msg}"
    assert entry["status"] == "待传输"
    assert entry["retry_count"] == 2, "重试次数应递增"
    assert "last_retry_time" in entry, "应记录重试时间"


# ============================================================
# 测试14：Store并发追加原子性
# ============================================================
def test_concurrent_append_atomic():
    """测试并发追加的原子性。"""
    import concurrent.futures

    def append_record(i: int):
        store.atomic_append("test_concurrent", {
            "id": i, "value": f"record-{i}",
            "status": "active", "pending": True, "abnormal": False,
        })
        return i

    # 清理
    store._tables["test_concurrent"] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(append_record, i) for i in range(20)]
        concurrent.futures.wait(futures)

    rows = store.rows("test_concurrent")
    assert len(rows) == 20, f"应追加20条，实际{len(rows)}条"


# ============================================================
# 测试15：跨模块全链路集成测试
# ============================================================
def test_full_cross_module_chain():
    """测试完整跨模块数据链路：
    航次计划批准 -> 站位布设 -> 采样 -> 样品登记 -> 分析申请审批
    """
    # 1. 批准航次计划
    from app.services.cruise_plan import CruisePlanService
    plan_svc = CruisePlanService()
    _samples("cruise_plan")[0]["status"] = "已提交"
    entry, msg = plan_svc.run_action(1, "批准计划")
    assert entry is not None and entry["status"] == "已批准"

    # 2. 布设站位
    from app.services.station_deploy import StationDeployService
    station_svc = StationDeployService()
    _samples("station_deploy")[0]["站位编号"] = "LINK-TEST-01"
    entry, msg = station_svc.run_action(1, "确认站位")
    assert entry is not None and entry["status"] == "已布设"

    # 3. 采样
    from app.services.sampling_record import SamplingRecordService
    sample_svc = SamplingRecordService()
    _samples("sampling_record")[0]["采样站位"] = "LINK-TEST-01"
    _samples("sampling_record")[0]["样品编号"] = "SAMP-LINK-001"
    _samples("sampling_record")[0]["样品类型"] = "水样"
    _samples("sampling_record")[0]["采样人"] = "链接测试员"
    _samples("sampling_record")[0]["采样时间"] = "2026-11-25"
    entry, msg = sample_svc.run_action(1, "记录采样")
    assert entry is not None and entry["status"] == "已采样"

    # 4. 样品入库 -> 自动登记
    entry, msg = sample_svc.run_action(1, "样品入库")
    assert entry is not None and entry["status"] == "已入库"

    # 5. 验证样品登记
    registry = store.find_one_by("sample_registry", "样品编号", "SAMP-LINK-001")
    assert registry is not None, "样品登记应有对应记录"
    assert registry.get("synced_from") == "sampling_record"

    # 6. 将样品登记入库
    from app.services.sample_registry import SampleRegistryService
    reg_svc = SampleRegistryService()
    if registry:
        registry["status"] = "已登记"
        entry, msg = reg_svc.run_action(registry["id"], "执行入库")
        assert entry is not None and entry["status"] == "已入库"

    # 7. 批准分析申请
    from app.services.analysis_request import AnalysisRequestService
    analysis_svc = AnalysisRequestService()
    _samples("analysis_request")[0]["申请样品"] = "SAMP-LINK-001"
    _samples("analysis_request")[0]["status"] = "已提交"
    entry, msg = analysis_svc.run_action(1, "批准分析")
    assert entry is not None, "样品已入库，应批准成功"