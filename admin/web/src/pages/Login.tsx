import { LockOutlined, MailOutlined } from "@ant-design/icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { App as AntApp, Button, Card, Form, Input, Typography } from "antd";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError, api, saveSession } from "../api";
import type { User } from "../types";

type LoginResult = {
  requires_2fa?: boolean;
  challenge?: string;
  email_masked?: string;
  message?: string;
  access_token?: string;
  refresh_token?: string;
  user?: User;
};

export function Login() {
  const navigate = useNavigate();
  const client = useQueryClient();
  const { message } = AntApp.useApp();
  const [challenge, setChallenge] = useState("");
  const [masked, setMasked] = useState("");

  function enter(data: LoginResult) {
    if (!data.access_token || !data.refresh_token || !data.user) {
      message.error("登录没有返回凭证");
      return;
    }
    saveSession(data.access_token, data.refresh_token);
    client.clear();
    client.setQueryData(["me"], data.user);
    navigate("/");
  }

  const login = useMutation({
    mutationFn: (values: { email: string; password: string }) =>
      api<LoginResult>("/v1/auth/login", {
        method: "POST",
        body: JSON.stringify(values),
      }),
    onSuccess: (data) => {
      if (data.requires_2fa) {
        setChallenge(data.challenge ?? "");
        setMasked(data.email_masked ?? "");
        message.info(data.message || "请输入身份验证器中的 6 位数字");
        return;
      }
      enter(data);
    },
    onError: (error) => message.error(error instanceof ApiError ? error.message : "登录失败"),
  });

  const confirm = useMutation({
    mutationFn: (values: { totp_code: string }) =>
      api<LoginResult>("/v1/auth/login/2fa", {
        method: "POST",
        body: JSON.stringify({ challenge, totp_code: values.totp_code }),
      }),
    onSuccess: (data) => enter(data),
    onError: (error) => message.error(error instanceof ApiError ? error.message : "验证失败"),
  });

  return (
    <div className="login-screen">
      <Card className="login-card">
        <div style={{ display: "flex", gap: 12, alignItems: "center", marginBottom: 20 }}>
          <div className="logo-mark">
            <img src="/logo.png" alt="" />
          </div>
          <div>
            <Typography.Title level={4} style={{ margin: 0 }}>
              御镜管理后台
            </Typography.Title>
            <Typography.Text type="secondary">使用客户端同一套账号登录</Typography.Text>
          </div>
        </div>
        {challenge ? (
          <Form
            layout="vertical"
            requiredMark={false}
            onFinish={(values: { totp_code: string }) => confirm.mutate(values)}
          >
            <Typography.Paragraph type="secondary">
              {masked ? `请输入 ${masked} 的身份验证器里的 6 位数字。` : "请输入身份验证器里的 6 位数字。"}
            </Typography.Paragraph>
            <Form.Item
              name="totp_code"
              label="验证码"
              rules={[{ required: true, pattern: /^\d{6}$/, message: "请输入 6 位数字验证码" }]}
            >
              <Input inputMode="numeric" maxLength={6} autoComplete="one-time-code" placeholder="6 位数字" />
            </Form.Item>
            <Button type="primary" htmlType="submit" block loading={confirm.isPending}>
              确认
            </Button>
            <Button
              type="link"
              block
              disabled={confirm.isPending}
              onClick={() => {
                setChallenge("");
                setMasked("");
              }}
            >
              返回登录
            </Button>
          </Form>
        ) : (
          <Form layout="vertical" requiredMark={false} onFinish={(values) => login.mutate(values)}>
            <Form.Item name="email" label="邮箱" rules={[{ required: true, type: "email", message: "请填写邮箱" }]}>
              <Input prefix={<MailOutlined />} placeholder="name@company.com" autoComplete="username" />
            </Form.Item>
            <Form.Item name="password" label="密码" rules={[{ required: true, message: "请填写密码" }]}>
              <Input.Password prefix={<LockOutlined />} autoComplete="current-password" />
            </Form.Item>
            <Button type="primary" htmlType="submit" block loading={login.isPending}>
              登录
            </Button>
          </Form>
        )}
      </Card>
    </div>
  );
}
