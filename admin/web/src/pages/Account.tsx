import { useMutation, useQueryClient } from "@tanstack/react-query";
import { App as AntApp, Button, Card, Col, Form, Input, Row } from "antd";
import { useOutletContext } from "react-router-dom";
import { ApiError, api } from "../api";
import type { User } from "../types";
import { PageHead } from "../ui";

export function Account() {
  const user = useOutletContext<User>();
  const { message } = AntApp.useApp();
  const client = useQueryClient();
  const [passwordForm] = Form.useForm();

  const saveName = useMutation({
    mutationFn: (name: string) => api<User>("/v1/auth/profile", { method: "PUT", body: JSON.stringify({ name }) }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["me"] });
      message.success("姓名已更新");
    },
    onError: (error) => message.error(error instanceof ApiError ? error.message : "保存失败"),
  });

  const savePassword = useMutation({
    mutationFn: (values: { old_password: string; new_password: string }) =>
      api<{ ok: boolean; relay_refreshed: boolean }>("/v1/auth/password", {
        method: "PUT",
        body: JSON.stringify(values),
      }),
    onSuccess: (result) => {
      passwordForm.resetFields();
      message.success(result.relay_refreshed ? "密码已更新" : "密码已修改。请退出后用新密码重新登录。");
    },
    onError: (error) => message.error(error instanceof ApiError ? error.message : "修改失败"),
  });

  return (
    <>
      <PageHead title="个人设置" />
      <Row gutter={[16, 16]}>
        <Col xs={24} lg={12}>
          <Card title="姓名">
            <Form
              layout="vertical"
              initialValues={{ name: user.name }}
              onFinish={(values: { name: string }) => saveName.mutate(values.name)}
            >
              <Form.Item label="邮箱">
                <Input value={user.email} disabled />
              </Form.Item>
              <Form.Item
                name="name"
                label="姓名"
                rules={[
                  { required: true, whitespace: true, message: "请填写姓名" },
                  { max: 40, message: "姓名最多 40 个字" },
                ]}
              >
                <Input placeholder="例如：张剪辑" maxLength={40} />
              </Form.Item>
              <Button type="primary" htmlType="submit" loading={saveName.isPending}>
                保存
              </Button>
            </Form>
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card title="登录密码">
            <Form
              form={passwordForm}
              layout="vertical"
              onFinish={(values: { old_password: string; new_password: string }) =>
                savePassword.mutate({ old_password: values.old_password, new_password: values.new_password })
              }
            >
              <Form.Item name="old_password" label="当前密码" rules={[{ required: true, message: "请填写当前密码" }]}>
                <Input.Password autoComplete="current-password" />
              </Form.Item>
              <Form.Item
                name="new_password"
                label="新密码"
                rules={[
                  { required: true, message: "请填写新密码" },
                  { min: 8, max: 72, message: "密码需要 8 到 72 位" },
                ]}
              >
                <Input.Password autoComplete="new-password" />
              </Form.Item>
              <Form.Item
                name="confirm"
                label="确认新密码"
                dependencies={["new_password"]}
                rules={[
                  { required: true, message: "请再输入一次新密码" },
                  ({ getFieldValue }) => ({
                    validator(_, value: string) {
                      if (!value || getFieldValue("new_password") === value) return Promise.resolve();
                      return Promise.reject(new Error("两次输入的密码不一致"));
                    },
                  }),
                ]}
              >
                <Input.Password autoComplete="new-password" />
              </Form.Item>
              <Button type="primary" htmlType="submit" loading={savePassword.isPending}>
                修改密码
              </Button>
            </Form>
          </Card>
        </Col>
      </Row>
    </>
  );
}
