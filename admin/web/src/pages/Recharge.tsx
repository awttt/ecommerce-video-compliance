import { Alert, Button, Card, InputNumber, Modal, QRCode, Spin, Statistic, Table, Typography, App as AntApp } from "antd";
import type { ColumnsType } from "antd/es/table";
import { CheckOutlined, InfoCircleOutlined, LinkOutlined, QrcodeOutlined, ScanOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import { useEffect, useRef, useState } from "react";
import { PageHead } from "../ui";
import { cancelRechargeOrder, createRecharge, getRechargeOrder, listRechargeOrders, localBalance, rechargeInfo } from "../local";
import type { RechargeInfo, RechargeMethod, RechargeOrder } from "../types";

const PRESETS = [10, 30, 50, 100, 200, 500];
const OPEN = new Set(["PENDING", "PAID", "RECHARGING"]);

const STATUS_LABEL: Record<string, string> = {
  PENDING: "待支付",
  PAID: "已支付",
  RECHARGING: "入账中",
  COMPLETED: "已到账",
  EXPIRED: "已过期",
  CANCELLED: "已取消",
  FAILED: "失败",
  REFUND_REQUESTED: "退款申请中",
  REFUNDING: "退款中",
  REFUND_PENDING: "退款处理中",
  PARTIALLY_REFUNDED: "部分退款",
  REFUNDED: "已退款",
  REFUND_FAILED: "退款失败",
};

function round2(value: number): number {
  return Math.round(value * 100) / 100;
}

function lowerBound(info: RechargeInfo, method: RechargeMethod | undefined): number {
  const values = [info.global_min, method?.single_min ?? 0].filter((value) => value > 0);
  return values.length ? Math.max(...values) : 0;
}

function upperBound(info: RechargeInfo, method: RechargeMethod | undefined): number {
  const values = [info.global_max, method?.single_max ?? 0].filter((value) => value > 0);
  return values.length ? Math.min(...values) : 0;
}

function defaultAmount(info: RechargeInfo, method: RechargeMethod | undefined): number {
  const min = lowerBound(info, method);
  const max = upperBound(info, method);
  let amount = 50;
  if (min > 0 && amount < min) amount = min;
  if (max > 0 && amount > max) amount = max;
  return amount;
}

function payUnit(currency: string): string {
  if (!currency || currency === "CNY") return "元";
  return currency;
}

function quote(amount: number, info: RechargeInfo): { pay: number; credit: number } {
  const multiplier = info.balance_recharge_multiplier > 0 ? info.balance_recharge_multiplier : 1;
  const fee = info.recharge_fee_rate > 0 ? amount * info.recharge_fee_rate / 100 : 0;
  return { pay: round2(amount + fee), credit: round2(amount * multiplier) };
}

function statusTag(status: string) {
  const label = STATUS_LABEL[status] ?? status;
  if (status === "COMPLETED") return <span className="pill pill--confirmed"><i className="status-dot" />{label}</span>;
  if (status === "PENDING" || status === "PAID" || status === "RECHARGING") {
    return <span className="pill pill--active"><i className="status-dot" />{label}</span>;
  }
  if (status === "FAILED" || status === "EXPIRED" || status === "REFUND_FAILED") {
    return <span className="pill pill--error"><i className="status-dot" />{label}</span>;
  }
  return <span className="pill pill--neutral">{label}</span>;
}

function stamp(value: string): string {
  if (!value) return "";
  const parsed = dayjs(value);
  return parsed.isValid() ? parsed.format("MM-DD HH:mm") : value;
}

interface PayTarget {
  orderId: number;
  code: string;
  url: string;
  payAmount: number;
  amount: number;
  currency: string;
  methodName: string;
}

export function Recharge() {
  const { message } = AntApp.useApp();
  const [info, setInfo] = useState<RechargeInfo | null>(null);
  const [orders, setOrders] = useState<RechargeOrder[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [amount, setAmount] = useState(50);
  const [paymentType, setPaymentType] = useState("");
  const [paying, setPaying] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [current, setCurrent] = useState<RechargeOrder | null>(null);
  const [payTarget, setPayTarget] = useState<PayTarget | null>(null);
  const [payOpen, setPayOpen] = useState(false);
  const [balanceError, setBalanceError] = useState("");
  const [account, setAccount] = useState<{ email: string; balance: number | null }>({ email: "", balance: null });
  const currentId = useRef(0);
  const told = useRef(0);
  const seeded = useRef(false);

  async function load() {
    setLoading(true);
    setError("");
    setBalanceError("");
    try {
      const [nextInfo, nextOrders, bal] = await Promise.all([
        rechargeInfo(),
        listRechargeOrders(),
        localBalance(),
      ]);
      setInfo(nextInfo);
      setOrders(nextOrders.orders);
      setAccount({ email: bal.email, balance: bal.balance });
      if (!bal.logged_in) {
        setBalanceError("登录状态已失效，请退出后重新登录再充值");
      } else {
        setBalanceError("");
      }
      if (!seeded.current) {
        seeded.current = true;
        const first = nextInfo.methods[0];
        setPaymentType(first?.payment_type ?? "");
        setAmount(defaultAmount(nextInfo, first));
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "充值信息加载失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load().catch(() => undefined);
  }, []);

  function accept(next: RechargeOrder) {
    if (currentId.current !== next.order_id) return;
    setCurrent((prev) => {
      const same = prev?.order_id === next.order_id ? prev : null;
      return {
        ...next,
        qr_code: next.qr_code || same?.qr_code || "",
        pay_url: next.pay_url || same?.pay_url || "",
        currency: next.currency || same?.currency || "",
      };
    });
    const polled = next.settings?.account_balance;
    if (typeof polled === "number") setAccount((prev) => ({ ...prev, balance: polled }));
    if (next.status && !OPEN.has(next.status)) {
      setPayOpen(false);
      setPayTarget(null);
    }
    if (next.status === "COMPLETED" && told.current !== next.order_id) {
      told.current = next.order_id;
      message.success("已到账");
      listRechargeOrders().then((page) => setOrders(page.orders)).catch(() => undefined);
      localBalance().then((bal) => setAccount({ email: bal.email, balance: bal.balance })).catch(() => undefined);
    }
  }

  useEffect(() => {
    if (!current || !OPEN.has(current.status)) return;
    const orderId = current.order_id;
    const timer = window.setInterval(() => {
      getRechargeOrder(orderId)
        .then(accept)
        .catch((caught: Error) => message.error(caught.message));
    }, 3000);
    return () => window.clearInterval(timer);
  }, [current?.order_id, current?.status, message]);

  const method = info?.methods.find((item) => item.payment_type === paymentType) ?? info?.methods[0];
  const min = info && method ? lowerBound(info, method) : 0;
  const max = info && method ? upperBound(info, method) : 0;
  const estimate = info ? quote(amount, info) : null;
  const unit = payUnit(method?.currency ?? "");
  const limitText = min > 0 && max > 0
    ? `${min} – ${max} ${unit}`
    : min > 0
      ? `最低 ${min} ${unit}`
      : max > 0
        ? `最高 ${max} ${unit}`
        : "不限";

  async function submit() {
    if (!method || paying || cancelling) return;
    setPaying(true);
    try {
      const next = await createRecharge(amount, method.payment_type);
      currentId.current = next.order_id;
      setCurrent(next);
      if (next.qr_code || next.pay_url) {
        setPayTarget({
          orderId: next.order_id,
          code: next.qr_code,
          url: next.pay_url,
          payAmount: next.pay_amount,
          amount: next.amount,
          currency: next.currency || method.currency,
          methodName: method.display_name,
        });
        setPayOpen(true);
      } else {
        setPayTarget(null);
        setPayOpen(false);
      }
      if (next.message) message.warning(next.message);
      const page = await listRechargeOrders();
      setOrders(page.orders);
    } catch (caught) {
      message.error(caught instanceof Error ? caught.message : "下单失败");
    } finally {
      setPaying(false);
    }
  }

  async function cancel() {
    if (!current || paying || cancelling) return;
    setCancelling(true);
    try {
      const next = await cancelRechargeOrder(current.order_id);
      currentId.current = next.order_id;
      setCurrent(next);
      setPayOpen(false);
      setPayTarget(null);
      const page = await listRechargeOrders();
      setOrders(page.orders);
    } catch (caught) {
      message.error(caught instanceof Error ? caught.message : "取消失败");
    } finally {
      setCancelling(false);
    }
  }

  const columns: ColumnsType<RechargeOrder> = [
    { title: "时间", dataIndex: "created_at", width: 116, render: (value: string) => stamp(value) },
    { title: "支付", dataIndex: "pay_amount", width: 100, align: "right", className: "col-num", render: (value: number) => value.toFixed(2) },
    { title: "到账", dataIndex: "amount", width: 100, align: "right", className: "col-num", render: (value: number) => value.toFixed(2) },
    {
      title: "方式",
      dataIndex: "payment_type",
      width: 120,
      render: (value: string) => info?.methods.find((item) => item.payment_type === value)?.display_name || value,
    },
    { title: "状态", dataIndex: "status", width: 110, render: (value: string) => statusTag(value) },
  ];

  if (loading) {
    return (
      <div className="recharge-page">
        <PageHead title="充值" />
        <div className="recharge-state"><Spin /><span>正在加载充值信息…</span></div>
      </div>
    );
  }

  if (balanceError) {
    return (
      <div className="recharge-page">
        <PageHead title="充值" />
        <Card>
          <Alert type="error" showIcon message="登录状态已失效，请退出后重新登录再充值" />
        </Card>
      </div>
    );
  }

  if (error) {
    return (
      <div className="recharge-page">
        <PageHead title="充值" />
        <Card>
          <Alert
            type="error"
            showIcon
            message="充值信息加载失败"
            description={error}
            action={<Button size="small" onClick={() => load().catch(() => undefined)}>重试</Button>}
          />
        </Card>
      </div>
    );
  }

  if (!info) {
    return (
      <div className="recharge-page">
        <PageHead title="充值" />
      </div>
    );
  }

  return (
    <div className="recharge-page">
      <PageHead title="充值" />

      <Card style={{ marginBottom: 16 }}>
        <Statistic title="当前余额" value={account.balance ?? 0} precision={2} prefix="¥" />
        {account.email ? <Typography.Text type="secondary">充值账号 {account.email}</Typography.Text> : null}
      </Card>

      {info.balance_disabled || info.methods.length === 0 ? (
        info.message
          ? <Alert className="recharge-alert" type="warning" showIcon message={info.message} />
          : null
      ) : info.message ? (
        <div className="recharge-notice">
          <InfoCircleOutlined />
          <span>{info.message}</span>
        </div>
      ) : null}

      <div className="recharge-grid">
        <div className="recharge-main">
          {info.methods.length > 0 && !info.balance_disabled ? (
            <Card className="recharge-card" title="充值金额">
              <div className="field">
                <div className="section-label">支付方式</div>
                <div className="choice-row">
                  {info.methods.map((item) => (
                    <button
                      key={item.payment_type}
                      type="button"
                      className={`choice${item.payment_type === method?.payment_type ? " is-selected" : ""}`}
                      onClick={() => {
                        setPaymentType(item.payment_type);
                        setAmount((currentAmount) => {
                          const nextMin = lowerBound(info, item);
                          const nextMax = upperBound(info, item);
                          if (nextMin > 0 && currentAmount < nextMin) return nextMin;
                          if (nextMax > 0 && currentAmount > nextMax) return nextMax;
                          return currentAmount;
                        });
                      }}
                    >
                      <span className="choice-check"><CheckOutlined /></span>
                      {item.display_name}
                    </button>
                  ))}
                </div>
              </div>

              <div className="field">
                <div className="section-label">
                  选择金额
                  <span className="field-hint">单笔 {limitText}</span>
                </div>
                <div className="amount-grid">
                  {PRESETS
                    .filter((value) => (min <= 0 || value >= min) && (max <= 0 || value <= max))
                    .map((value) => (
                      <button
                        key={value}
                        type="button"
                        className={`amount-option${amount === value ? " is-selected" : ""}`}
                        onClick={() => setAmount(value)}
                      >
                        <span className="amount-option__value">
                          <strong>{value}</strong>
                          <span className="amount-option__unit">{unit}</span>
                        </span>
                      </button>
                    ))}
                </div>
                <div className="custom-amount">
                  <span>自定义金额</span>
                  <InputNumber
                    min={min > 0 ? min : 0.01}
                    max={max > 0 ? max : 1_000_000}
                    step={1}
                    value={amount}
                    addonAfter={unit}
                    onChange={(value) => {
                      if (typeof value === "number" && value > 0) setAmount(value);
                    }}
                  />
                </div>
              </div>

              {estimate ? (
                <div className="pay-summary">
                  <div className="pay-summary__main">
                    <span>预计支付</span>
                    <div className="pay-summary__amount">
                      {unit === "元" ? <small>¥</small> : null}
                      {estimate.pay.toFixed(2)}
                      {unit !== "元" ? <em>{unit}</em> : null}
                    </div>
                  </div>
                  <div className="pay-summary__side">
                    <div>
                      <span>到账额度</span>
                      <strong>{estimate.credit.toFixed(2)}</strong>
                    </div>
                  </div>
                </div>
              ) : null}
              <div className="pay-note">实际金额以下单结果为准</div>
              <Button
                className="pay-submit"
                type="primary"
                size="large"
                block
                loading={paying}
                disabled={!method || amount <= 0 || cancelling}
                onClick={() => submit().catch(() => undefined)}
              >
                去支付
              </Button>
            </Card>
          ) : null}

          {current ? (
            <Card
              className="recharge-card order-card"
              title={
                <div className="card-title-row">
                  当前订单
                  <span className="title-sub">#{current.order_id}</span>
                  {statusTag(current.status)}
                </div>
              }
            >
              <div className="order-figures">
                <div>
                  <span>支付金额</span>
                  <strong>{current.pay_amount.toFixed(2)} <small>{payUnit(current.currency)}</small></strong>
                </div>
                <div>
                  <span>到账额度</span>
                  <strong>{current.amount.toFixed(2)}</strong>
                </div>
              </div>

              {current.message ? <Alert type="warning" showIcon message={current.message} /> : null}

              {payTarget && payTarget.orderId === current.order_id && OPEN.has(current.status) ? (
                <div className="order-pay-entry">
                  <Button
                    type="primary"
                    icon={<QrcodeOutlined />}
                    onClick={() => setPayOpen(true)}
                  >
                    查看支付二维码
                  </Button>
                  <span className="hint">扫码或打开支付页完成付款</span>
                </div>
              ) : null}

              {OPEN.has(current.status) ? (
                <div className="order-foot">
                  <span className="hint">支付完成后余额会自动更新，无需刷新页面。</span>
                  {current.status === "PENDING" ? (
                    <Button type="text" danger loading={cancelling} disabled={paying} onClick={() => cancel().catch(() => undefined)}>
                      取消订单
                    </Button>
                  ) : null}
                </div>
              ) : null}
            </Card>
          ) : null}

        </div>

        <aside className="recharge-side">
          <Card className="recharge-card side-card" title="支付说明">
            <dl className="info-list">
              <div><dt>支付币种</dt><dd>{unit}</dd></div>
              <div><dt>单笔限额</dt><dd>{limitText}</dd></div>
              {method?.daily_limit && method.daily_limit > 0 ? (
                <div><dt>每日限额</dt><dd>{method.daily_limit} {unit}</dd></div>
              ) : null}
            </dl>
            {info.help_text ? <div className="help-text">{info.help_text}</div> : null}
            <div className="pay-steps">
              <span className="side-lead">支付流程</span>
              <ol>
                <li>选择金额，确认预计支付与到账额度</li>
                <li>扫码或打开支付页完成付款</li>
                <li>到账后余额自动更新，可在记录中查看状态</li>
              </ol>
            </div>
          </Card>
        </aside>
      </div>

      <Card
        className="recharge-card records-card"
        title={<div className="card-title-row">充值记录<span className="title-sub">{orders.length} 笔</span></div>}
      >
        <Table
          size="small"
          rowKey="order_id"
          columns={columns}
          dataSource={orders}
          pagination={false}
          locale={{ emptyText: "还没有充值记录" }}
        />
      </Card>

      <Modal
        className="pay-modal"
        open={payOpen && !!payTarget}
        centered
        width={420}
        footer={null}
        title={null}
        onCancel={() => setPayOpen(false)}
      >
        {payTarget ? (
          <div className="pay-dialog">
            <div className="pay-dialog__head">
              <span className="pay-dialog__eyebrow">扫码支付</span>
              <div className="pay-dialog__amount">
                {payUnit(payTarget.currency) === "元" ? <small>¥</small> : null}
                {payTarget.payAmount.toFixed(2)}
                {payUnit(payTarget.currency) !== "元" ? <em>{payUnit(payTarget.currency)}</em> : null}
              </div>
              <div className="pay-dialog__sub">
                到账 {payTarget.amount.toFixed(2)} · 订单 #{payTarget.orderId}
              </div>
            </div>

            {payTarget.code ? (
              <div className="pay-dialog__qr">
                <div className="qr-frame"><QRCode value={payTarget.code} size={188} /></div>
                <div className="qr-caption"><ScanOutlined /> 请用{payTarget.methodName}扫码支付</div>
              </div>
            ) : (
              <div className="pay-dialog__link">
                <LinkOutlined />
                <span>当前支付方式需要在网页中完成付款</span>
              </div>
            )}

            {current?.order_id === payTarget.orderId ? (
              <div className="pay-dialog__status">
                {statusTag(current.status)}
                <span>每 3 秒自动检查支付状态</span>
              </div>
            ) : null}

            <div className="pay-dialog__actions">
              {payTarget.url ? (
                <Button block icon={<LinkOutlined />} onClick={() => window.open(payTarget.url, "_blank", "noopener,noreferrer")}>
                  打开支付页面
                </Button>
              ) : null}
              {current?.order_id === payTarget.orderId && current.status === "PENDING" ? (
                <Button block danger loading={cancelling} disabled={paying} onClick={() => cancel().catch(() => undefined)}>
                  取消订单
                </Button>
              ) : null}
              <Button block type="text" onClick={() => setPayOpen(false)}>
                关闭
              </Button>
            </div>

            <div className="pay-dialog__foot">支付完成后余额自动更新，无需刷新页面</div>
          </div>
        ) : null}
      </Modal>
    </div>
  );
}
