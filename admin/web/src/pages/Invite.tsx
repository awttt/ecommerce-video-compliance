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
      api<{ warning: string }>(`/v1/invites/${token}/accept`, {
        method: "POST",
        body: JSON.stringify(values),
      }),
    onSuccess: (data) => {
      setDone(data.warning || "");
      message.success(data.warning ? `已开通。${data.warning}` : "已开通，请用这个邮箱和密码登录御镜客户端。");
    },
    onError: (error) => message.error(error instanceof ApiError ? error.message : "开通失败"),
  });

  return (
    <div className="login-screen">
      <Card className="login-card">
        <Typography.Title level={4}>接受邀请</Typography.Title>
        <Typography.Paragraph type="secondary">
          邮件里只有这张链接，没有初始密码。设好密码后才会创建中转站账号，并且只分配专属分组。
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
