import { useMutation } from "@tanstack/react-query";
import { App as AntApp, Button, Card, Form, Input, Result, Typography } from "antd";
import { useState } from "react";
import { useParams } from "react-router-dom";
import { ApiError, api } from "../api";

export function AcceptInvite() {
  const { token = "" } = useParams();
  const { message } = AntApp.useApp();
  const [done, setDone] = useState("");
  const accept = useMutation({
    mutationFn: (values: { password: string; name: string }) =>
      api<{ warning: string; linked_existing?: boolean }>(`/v1/invites/${token}/accept`, {
        method: "POST",
        body: JSON.stringify(values),
      }),
    onSuccess: (data) => {
      const text = data.linked_existing
        ? "已绑定原有中转站账号，请用原来的密码登录。"
        : "已开通，请用这个邮箱和刚才的密码登录。";
      setDone(data.warning ? `${text}${data.warning}` : text);
      message.success(data.warning ? `已开通。${data.warning}` : text);
    },
    onError: (error) => message.error(error instanceof ApiError ? error.message : "开通失败"),
  });

  return (
    <div className="login-screen">
      <Card className="login-card">
        <Typography.Title level={4}>接受邀请</Typography.Title>
        <Typography.Paragraph type="secondary">
          系统会按邮箱自动识别。还没有中转站账号时，用这里设置的密码开通并绑定；已经注册过的账号会直接绑定，沿用原来的密码。
        </Typography.Paragraph>
        {accept.isSuccess ? (
          <Result
            status="success"
            title="账号已开通"
            subTitle={done || "请回到御镜客户端，用这个邮箱和刚才的密码登录。"}
          />
        ) : (
          <Form layout="vertical" onFinish={(values) => accept.mutate(values)}>
            <Form.Item name="name" label="姓名">
              <Input placeholder="怎么称呼你" />
            </Form.Item>
            <Form.Item name="password" label="密码" rules={[{ required: true, min: 8, message: "密码至少 8 位" }]}>
              <Input.Password />
            </Form.Item>
            <Button type="primary" htmlType="submit" loading={accept.isPending}>
              开通
            </Button>
          </Form>
        )}
      </Card>
    </div>
  );
}
