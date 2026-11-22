import { Alert, Button, Card, Form, Input, Space, Typography } from "antd";
import { useEffect, useState, type ReactNode } from "react";
import {
  accountLogin,
  accountLogin2fa,
  accountOptions,
  accountRegister,
  accountSendCode,
} from "./api";
import type { AccountOptions, Settings } from "./types";

const { Paragraph } = Typography;

function AuthLogo() {
  return <img className="brand-logo" src="/logo.png" alt="" />;
}

export function formatBalance(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return value.toFixed(4);
}

function useSiteOptions() {
  const [options, setOptions] = useState<AccountOptions | null>(null);
  const [optionsError, setOptionsError] = useState("");

  useEffect(() => {
    let cancelled = false;
    accountOptions()
      .then((next) => {
        if (!cancelled) setOptions(next);
      })
      .catch((error: Error) => {
        if (!cancelled) setOptionsError(error.message);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return { options, optionsError };
}

function AuthFrame({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="auth-page">
      <Card className="auth-card">
        <div className="auth-brand">
          <span className="auth-logo"><AuthLogo /></span>
          <strong>御镜视频审核系统</strong>
          <span>识别画面与口播中的价格、规格、品牌，以及口播里的夸大宣传</span>
        </div>
        <div className="auth-title">{title}</div>
        {children}
      </Card>
    </div>
  );
}

function SiteNotices({ options, optionsError }: { options: AccountOptions | null; optionsError: string }) {
  const captchaOn = Boolean(options && options.captcha && options.captcha !== "none");
  return (
    <>
      {optionsError ? <Alert type="error" showIcon style={{ marginBottom: 12 }} message={optionsError} /> : null}
      {options && !options.reachable && options.message ? (
        <Alert type="error" showIcon style={{ marginBottom: 12 }} message={options.message} />
      ) : null}
      {options?.reachable && options.message ? (
        <Alert type="warning" showIcon style={{ marginBottom: 12 }} message={options.message} />
      ) : null}
      {captchaOn ? (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="站点开启了人机验证，这个窗口里填不了验证码。请先在大语言模型后台关闭验证码。"
        />
      ) : null}
    </>
  );
}

export function LoginPage({
  rememberedEmail,
  onLoggedIn,
  onGoRegister,
}: {
  rememberedEmail?: string;
  onLoggedIn: (settings: Settings, message?: string) => void;
  onGoRegister: () => void;
}) {
  const { options, optionsError } = useSiteOptions();
  const [loginBusy, setLoginBusy] = useState(false);
  const [twoFactorBusy, setTwoFactorBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [noticeError, setNoticeError] = useState(false);
  const [needs2fa, setNeeds2fa] = useState(false);
  const [masked, setMasked] = useState("");

  async function finish(result: { settings: Settings; requires_2fa: boolean; email_masked: string; message: string }) {
    setNoticeError(false);
    setNotice(result.message);
    if (result.requires_2fa) {
      setNeeds2fa(true);
      setMasked(result.email_masked);
      return;
    }
    if (result.settings.account_logged_in) onLoggedIn(result.settings, result.message);
  }

  return (
    <AuthFrame title={needs2fa ? "两步验证" : "登录"}>
      <SiteNotices options={options} optionsError={optionsError} />
      {notice ? (
        <Alert type={noticeError ? "error" : "info"} showIcon style={{ marginBottom: 12 }} message={notice} />
      ) : null}
      {needs2fa ? (
        <Form
          layout="vertical"
          requiredMark={false}
          onFinish={async (values: { totp_code: string }) => {
            setTwoFactorBusy(true);
            try {
              await finish(await accountLogin2fa(values.totp_code));
            } catch (error) {
              setNoticeError(true);
              setNotice(error instanceof Error ? error.message : "验证失败");
            } finally {
              setTwoFactorBusy(false);
            }
          }}
        >
          <Paragraph type="secondary">
            {masked ? `请输入 ${masked} 的身份验证器里的 6 位数字。` : "请输入身份验证器里的 6 位数字。"}
          </Paragraph>
          <Form.Item
            name="totp_code"
            label="验证码"
            rules={[{ required: true, pattern: /^\d{6}$/, message: "请输入 6 位数字验证码" }]}
          >
            <Input inputMode="numeric" maxLength={6} autoComplete="one-time-code" placeholder="6 位数字" />
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={twoFactorBusy}>
            确认
          </Button>
          <Button type="link" block disabled={twoFactorBusy} onClick={() => { setNeeds2fa(false); setNotice(""); }}>
            返回登录
          </Button>
        </Form>
      ) : (
        <Form
          layout="vertical"
          requiredMark={false}
          initialValues={{ email: rememberedEmail || "" }}
          onFinish={async (values: { email: string; password: string }) => {
            setLoginBusy(true);
            try {
              await finish(await accountLogin(values.email, values.password));
            } catch (error) {
              setNoticeError(true);
              setNotice(error instanceof Error ? error.message : "登录失败");
            } finally {
              setLoginBusy(false);
            }
          }}
        >
          <Form.Item
            name="email"
            label="邮箱"
            rules={[{ required: true, type: "email", message: "请填写邮箱" }]}
          >
            <Input autoComplete="username" placeholder="name@example.com" />
          </Form.Item>
          <Form.Item name="password" label="密码" rules={[{ required: true, message: "请填写密码" }]}>
            <Input.Password autoComplete="current-password" />
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={loginBusy}>
            登录
          </Button>
          {options?.registration_enabled === false ? (
            <div className="auth-foot">站点已关闭注册。</div>
          ) : (
            <Button type="link" block onClick={onGoRegister}>
              没有账号？去注册
            </Button>
          )}
        </Form>
      )}
    </AuthFrame>
  );
}

export function RegisterPage({
  onRegistered,
  onGoLogin,
}: {
  onRegistered: (settings: Settings, message?: string) => void;
  onGoLogin: () => void;
}) {
  const { options, optionsError } = useSiteOptions();
  const [form] = Form.useForm();
  const [busy, setBusy] = useState(false);
  const [sending, setSending] = useState(false);
  const [notice, setNotice] = useState("");
  const [noticeError, setNoticeError] = useState(false);
  const verifyRequired = Boolean(options?.email_verify_enabled);
  const registrationClosed = options?.registration_enabled === false;

  async function sendCode() {
    try {
      const values = await form.validateFields(["email"]);
      const email = String(values.email || "");
      setSending(true);
      const result = await accountSendCode(email);
      setNoticeError(false);
      setNotice(result.message);
    } catch (error) {
      if (error && typeof error === "object" && "errorFields" in error) return;
      setNoticeError(true);
      setNotice(error instanceof Error ? error.message : "验证码发送失败");
    } finally {
      setSending(false);
    }
  }

  return (
    <AuthFrame title="注册">
      <SiteNotices options={options} optionsError={optionsError} />
      {registrationClosed ? (
        <Alert type="warning" showIcon style={{ marginBottom: 12 }} message="站点已关闭注册。" />
      ) : null}
      {notice ? (
        <Alert type={noticeError ? "error" : "info"} showIcon style={{ marginBottom: 12 }} message={notice} />
      ) : null}
      <Form
        form={form}
        layout="vertical"
        requiredMark={false}
        onFinish={async (values: { email: string; password: string; verify_code?: string }) => {
          setBusy(true);
          try {
            const result = await accountRegister(values.email, values.password, values.verify_code || "");
            setNoticeError(false);
            setNotice(result.message);
            if (result.settings.account_logged_in) onRegistered(result.settings, result.message);
          } catch (error) {
            setNoticeError(true);
            setNotice(error instanceof Error ? error.message : "注册失败");
          } finally {
            setBusy(false);
          }
        }}
      >
        <Form.Item
          name="email"
          label="邮箱"
          rules={[{ required: true, type: "email", message: "请填写邮箱" }]}
        >
          <Input autoComplete="username" placeholder="name@example.com" />
        </Form.Item>
        <Form.Item
          name="password"
          label="密码"
          rules={[{ required: true, min: 6, message: "密码至少 6 位" }]}
        >
          <Input.Password autoComplete="new-password" />
        </Form.Item>
        <Form.Item
          name="confirm"
          label="确认密码"
          dependencies={["password"]}
          rules={[
            { required: true, message: "请再次输入密码" },
            ({ getFieldValue }) => ({
              validator(_, value) {
                if (!value || getFieldValue("password") === value) return Promise.resolve();
                return Promise.reject(new Error("两次输入的密码不一致"));
              },
            }),
          ]}
        >
          <Input.Password autoComplete="new-password" />
        </Form.Item>
        <Form.Item
          label="邮箱验证码"
          extra={
            options == null
              ? "正在确认站点是否要求验证邮箱"
              : verifyRequired
                ? "站点要求验证邮箱。先点发送验证码，再注册。"
                : "站点不要求时可以留空"
          }
          required={verifyRequired}
        >
          <Space.Compact style={{ width: "100%" }}>
            <Form.Item
              name="verify_code"
              noStyle
              rules={verifyRequired ? [{ required: true, message: "请填写邮箱验证码" }] : []}
            >
              <Input autoComplete="one-time-code" placeholder="验证码" />
            </Form.Item>
            <Button htmlType="button" loading={sending} disabled={registrationClosed} onClick={() => void sendCode()}>
              发送验证码
            </Button>
          </Space.Compact>
        </Form.Item>
        <Button type="primary" htmlType="submit" block loading={busy} disabled={registrationClosed}>
          注册
        </Button>
        <div className="auth-foot">
          已有账号？<Button type="link" onClick={onGoLogin}>去登录</Button>
        </div>
      </Form>
    </AuthFrame>
  );
}
