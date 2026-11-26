import random;
import unittest;
from dataclasses import replace;

import market_auction as ma;


class 报价函数测试(unittest.TestCase):
	# 报价环节的边界：技能、预算、质量、信誉、故意压价。

	def setUp(self):
		# 关掉报价抖动，让报价只受负载、成本、时延和质量影响。
		ma.V_报价抖动幅度 = 0.0;
		self.task = ma.Task("T001", "数据清洗", 5.0, 100.0, 0.5, 120.0, 1);
		self.agent = ma.Agent("agent_test", {"数据清洗": 0.9}, 2.0, 2000, 0.9, 20.0, 0.9);

	def test_负载越高报价越贵(self):
		# 同一 Agent 负载从 0.1 升到 0.8，报价应该明显上涨。
		self.agent.load = 0.1;
		low_bid = ma.计算报价(self.agent, self.task, random.Random(1));
		self.agent.load = 0.8;
		high_bid = ma.计算报价(self.agent, self.task, random.Random(1));
		self.assertGreater(high_bid.price, low_bid.price);

	def test_能力不匹配被驳回(self):
		# 没有对应技能的 Agent 不能参与该任务竞拍。
		bid = ma.计算报价(self.agent, replace(self.task, capability="图像识别"), random.Random(1));
		self.assertFalse(bid.valid);
		self.assertIn("能力不匹配", bid.note);

	def test_超出预算被驳回(self):
		# 报价超过任务预算上限时直接失效。
		bid = ma.计算报价(self.agent, replace(self.task, budget=1.0), random.Random(1));
		self.assertFalse(bid.valid);
		self.assertIn("预算", bid.note);

	def test_恶意低价被驳回(self):
		# 让 Agent 一定触发低价抢单，报价应被判定为异常并拦截。
		self.agent.game_probability = 1.0;
		bid = ma.计算报价(self.agent, self.task, random.Random(1));
		self.assertFalse(bid.valid);
		self.assertIn("异常低价", bid.note);

	def test_质量不足被驳回(self):
		# 技能太差导致预估质量低于门槛时，Agent 不应接单。
		self.agent.skills = {"数据清洗": 0.3};
		bid = ma.计算报价(self.agent, self.task, random.Random(1));
		self.assertFalse(bid.valid);
		self.assertIn("质量预估不达标", bid.note);

	def test_信誉过低被停牌(self):
		# 信誉跌破红线的 Agent 本轮不能报价。
		self.agent.reputation = 0.2;
		bid = ma.计算报价(self.agent, self.task, random.Random(1));
		self.assertFalse(bid.valid);
		self.assertIn("信誉低于红线", bid.note);


class 拍卖机制测试(unittest.TestCase):
	# 三种机制怎么选人、怎么定价。

	def setUp(self):
		self.task = ma.Task("T001", "数据清洗", 5.0, 100.0, 0.5, 120.0, 1);
		self.bids = [
			ma.Bid("agent_A", 30.0, 2000, 0.90, 0.90, True, ""),
			ma.Bid("agent_B", 20.0, 4000, 0.80, 0.80, True, ""),
			ma.Bid("agent_C", 25.0, 3000, 0.70, 0.60, False, "异常低价，疑似恶意抢单"),
		];

	def test_最低价只比较有效报价(self):
		# 被驳回的低价不能捡漏，agent_B 的 20 元报价应该中标。
		winner, payment = ma.最低价中标(self.task, self.bids, random.Random(1));
		self.assertEqual(winner.agent_id, "agent_B");
		self.assertAlmostEqual(payment, 20.0);

	def test_维克里按第二低价结算(self):
		# 中标者是 agent_B，但成交价应取第二低的 30 元。
		winner, payment = ma.维克里中标(self.task, self.bids, random.Random(1));
		self.assertEqual(winner.agent_id, "agent_B");
		self.assertAlmostEqual(payment, 30.0);

	def test_维克里只有一家投标时按自家报价结算(self):
		# 只有一条有效报价时没有第二低价，只能按自己的报价成交。
		single_bids = [bid for bid in self.bids if bid.valid][:1];
		winner, payment = ma.维克里中标(self.task, single_bids, random.Random(1));
		self.assertAlmostEqual(payment, winner.price);

	def test_综合评分更看重质量与信誉(self):
		# agent_A 更贵，但质量和信誉占优，综合得分应该最高。
		winner, payment = ma.综合评分中标(self.task, self.bids, random.Random(1));
		self.assertEqual(winner.agent_id, "agent_A");
		self.assertAlmostEqual(payment, 30.0);

	def test_无人投标返回空结果(self):
		# 全部报价无效时应返回 (None, 0.0)，交给上层兜底。
		winner, payment = ma.最低价中标(self.task, self.bids[2:], random.Random(1));
		self.assertIsNone(winner);
		self.assertAlmostEqual(payment, 0.0);


class 结算与兜底测试(unittest.TestCase):
	# 交割、返工、备用兜底和恶意报价的后果。

	def setUp(self):
		self.task = ma.Task("T001", "数据清洗", 5.0, 100.0, 0.5, 120.0, 1);
		self.agent = ma.Agent("agent_test", {"数据清洗": 0.9}, 2.0, 2000, 0.9, 20.0, 0.9);
		self.bids = [ma.Bid("agent_test", 20.0, 2000, 0.95, 0.9, True, "")];

	def test_中标后收入成本与负载同步更新(self):
		# 成交价 20，真实成本 5 小时乘 2 元，负载增加 5 除 20。
		record = ma.结算(self.task, self.bids[0], 20.0, [self.agent], self.bids, random.Random(1));
		self.assertEqual(record.status, "完成");
		self.assertAlmostEqual(self.agent.revenue, 20.0);
		self.assertAlmostEqual(self.agent.cost, 10.0);
		self.assertAlmostEqual(self.agent.load, 0.25);

	def test_质量不达标触发返工扣款(self):
		# 交付质量远低于门槛时，只结算六成报酬并扣信誉。
		low_quality_bids = [ma.Bid("agent_test", 20.0, 2000, 0.10, 0.9, True, "")];
		record = ma.结算(self.task, low_quality_bids[0], 20.0, [self.agent], low_quality_bids, random.Random(1));
		self.assertEqual(record.status, "返工");
		self.assertAlmostEqual(self.agent.revenue, 12.0);
		self.assertLess(self.agent.reputation, 0.9);

	def test_无人竞标由备用Agent接单(self):
		# 常规 Agent 都不投标时，备用 Agent 按成本加价兜底。
		backup = ma.Agent("agent_backup", {"数据清洗": 0.5}, 3.2, 8000, 0.92, 50.0, 1.0, is_backup=True);
		record = ma.兜底处理(self.task, [backup], [], random.Random(1));
		self.assertEqual(record.status, "兜底");
		self.assertEqual(record.winner, "agent_backup");
		self.assertAlmostEqual(record.payment, 25.6);

	def test_备用Agent过载转人工接管(self):
		# 备用 Agent 负载也满了，就只能转人工，不能硬塞任务。
		backup = ma.Agent("agent_backup", {"数据清洗": 0.5}, 3.2, 8000, 0.92, 50.0, 1.0, is_backup=True, load=0.95);
		record = ma.兜底处理(self.task, [backup], [], random.Random(1));
		self.assertEqual(record.status, "人工接管");
		self.assertAlmostEqual(record.payment, 0.0);

	def test_恶意报价会被扣信誉并计入拦截(self):
		# 故意低价抢单的 Agent 即使没中标也要扣信誉，本轮记录里统计拦截次数。
		cheater = ma.Agent("agent_cheat", {"数据清洗": 0.9}, 2.0, 2000, 0.9, 20.0, 0.9, game_probability=1.0);
		record = ma.执行一轮拍卖(self.task, [cheater], "最低价", random.Random(1));
		self.assertEqual(record.blocked_count, 1);
		self.assertLess(cheater.reputation, 0.9);


class 整体流程测试(unittest.TestCase):
	# 任务生成、机制注册和同种子复现。

	def test_任务编号唯一且轮次合法(self):
		# 任务编号不能重复，到达轮次必须落在模拟区间内。
		tasks = ma.生成任务列表(random.Random(3), 12);
		self.assertEqual(len({task.task_id for task in tasks}), len(tasks));
		self.assertTrue(all(1 <= task.arrive_round <= 12 for task in tasks));

	def test_三种机制都能完整跑完(self):
		# 三种机制都应该产出记录，并且交付率不会为零。
		for mechanism_name in ma.V_拍卖机制:
			report = ma.运行市场(ma.复制任务列表(ma.生成任务列表(random.Random(5), 10)), ma.构建异构Agent(), mechanism_name, 5, 10);
			self.assertGreater(len(report.records), 0);
			self.assertGreater(report.metrics["交付率"], 0.0);

	def test_同种子结果完全一致(self):
		# 固定种子后，两次模拟的总报酬和完成单数必须一致。
		tasks = ma.生成任务列表(random.Random(7), 10);
		first_report = ma.运行市场(ma.复制任务列表(tasks), ma.构建异构Agent(), "综合评分", 7, 10);
		second_report = ma.运行市场(ma.复制任务列表(tasks), ma.构建异构Agent(), "综合评分", 7, 10);
		self.assertEqual(first_report.metrics["总报酬"], second_report.metrics["总报酬"]);
		self.assertEqual(first_report.metrics["完成"], second_report.metrics["完成"]);


if __name__ == "__main__":
	unittest.main();