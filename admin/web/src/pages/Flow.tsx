import { Alert, Card, Steps, Table, Tag, Typography } from "antd";
import { PageHead } from "../ui";

const FLOWS = [
  {
    title: "开通成员",
    note: "邮件里只有链接。对方设密码后才建中转站账号，并且只分配专属分组。分组不在可用列表里时，客户端不建画面密钥，提示联系管理员。",
    steps: ["填写邮箱和角色", "校验席位", "对方设密码", "只建专属分组账号", "写入成员"],
  },
  {
    title: "分配额度",
    note: "先在本地占住幂等键，再给中转站加款。对账只看：成员当前余额合计 + 本月已消耗 = 已分配累计。退回用减法，不把这笔再加进消耗。",
    steps: ["提交成员和金额", "校验公司池", "中转站加款或扣款", "改本地余额", "写审计"],
  },
  {
    title: "命中词",
    note: "全局词由平台管理员审，公司词由本公司管理员审。不能审自己提交的词。通过后和客户端内置词合并，已经排队的视频仍用当时的规则。",
    steps: ["客户端或后台提交", "待审", "审批", "版本号 +1", "按 since 增量拉取"],
  },
  {
    title: "用量归集",
    note: "日期按 Asia/Shanghai。cost 已含 1.8 倍分组倍率，不可再次乘。看板只读本地快照。",
    steps: ["按成员取区间用量", "写入每日快照", "看板只读本地", "对账状态"],
  },
  {
    title: "登录与算力",
    note: "后台和客户端都用成员自己的中转站账号。后台在服务端完成抽帧、画面和口播识别，直接连 gateway 扣费；这里只登记任务、保存结果，不代理其他算力。",
    steps: ["登录中转站", "后台队列识别", "直连 gateway 扣费", "结果留在后台"],
  },
];

const MATRIX = [
  ["建 / 停用公司、设席位", "可以", "—", "—"],
  ["邀请 / 禁用成员", "可以", "本公司", "—"],
  ["分配 / 退回额度", "可以", "不超公司池", "—"],
  ["查看用量", "全部", "本公司", "仅自己"],
  ["上传视频、看命中、导出", "全部公司", "本公司", "仅自己的视频"],
  ["提交命中词", "可以", "可以", "可以"],
  ["审核命中词", "全局和任意公司", "仅本公司", "—"],
  ["查看审计", "全部", "本公司", "仅自己的提交"],
  ["改中转站地址", "可以", "—", "—"],
];

function cell(value: string) {
  if (value === "—") return <Typography.Text type="secondary">—</Typography.Text>;
  if (value === "可以" || value === "全部" || value === "全部公司" || value === "全局和任意公司") return <Tag color="success">{value}</Tag>;
  if (value.startsWith("仅")) return <Tag>{value}</Tag>;
  return <Tag color="processing">{value}</Tag>;
}

export function Flow() {
  return (
    <>
      <PageHead title="流程图与权限" description="五个关键流程，加上三角色能做什么。" />
      {FLOWS.map((flow, index) => (
        <Card key={flow.title} title={`${index + 1}. ${flow.title}`} style={{ marginBottom: 16 }}>
          <Steps
            size="small"
            items={flow.steps.map((title) => ({ title }))}
            style={{ marginBottom: 12 }}
          />
          <Alert type="info" showIcon message={flow.note} />
        </Card>
      ))}
      <Card title="角色权限">
        <Table
          pagination={false}
          rowKey={(row) => row[0]}
          dataSource={MATRIX}
          columns={[
            { title: "操作", render: (_value, row) => row[0] },
            { title: "平台管理员", render: (_value, row) => cell(row[1]) },
            { title: "公司管理员", render: (_value, row) => cell(row[2]) },
            { title: "普通用户", render: (_value, row) => cell(row[3]) },
          ]}
        />
      </Card>
    </>
  );
}