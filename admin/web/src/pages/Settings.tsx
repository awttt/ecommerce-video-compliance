import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App as AntApp, Button, Card, Col, Form, Input, Row, Space, Switch, Table, Tag } from "antd";
import { ApiError, api } from "../api";
import type { Settings as SettingsData } from "../types";
import { PageHead, StatusTag } from "../ui";

export function Settings() {
  const { message } = AntApp.useApp();
  const client = useQueryClient();
  const query = useQuery({ queryKey: ["settings"], queryFn: () => api<SettingsData>("/v1/settings") });
  const data = query.data;

  const save = useMutation({
    mutationFn: (values: SettingsData) => api<SettingsData>("/v1/settings", { method: "PUT", body: JSON.stringify(values) }),
    onSuccess: () => {
      message.success("已保存。管理密钥仍然只在服务端环境变量里。");
      void client.invalidateQueries({ queryKey: ["settings"] });
    },
    onError: (error) => message.error(error instanceof ApiError ? error.message : "保存失败"),
  });

  const test = useMutation({
    mutationFn: () => api<{ message: string }>("/v1/settings/test", { method: "POST" }),
    onSuccess: (result) => message.info(result.message),
    onError: (error) => message.error(error instanceof ApiError ? error.message : "测试失败"),
  });

  return (
    <>
      <PageHead title="接入设置" description="中转站地址和分组可以改。管理密钥只存在服务端环境变量，页面上看不到，也不会下发给客户端。" />
      <Row gutter={[16, 16]}>
        <Col xs={24} lg={14}>
          <Card title="中转站连接">
            {data ? (
              <Form
                layout="vertical"
                initialValues={data}
                onFinish={(values) =>
                  save.mutate({
                    ...data,
                    ...values,
                  })
                }
              >
                <Form.Item name="relay_base_url" label="API 地址" rules={[{ required: true, message: "请填写地址" }]}>
                  <Input />
                </Form.Item>
                <Form.Item name="relay_gateway_url" label="Gateway 地址" rules={[{ required: true, message: "请填写地址" }]}>
                  <Input />
                </Form.Item>
                <Form.Item name="group_name" label="专属分组名称" rules={[{ required: true, message: "请填写分组" }]}>
                  <Input />
                </Form.Item>
                <Form.Item name="group_id" label="审核专属分组 ID" extra="需要填写 token4u 中现有审核专属分组的数字 ID。">
                  <Input />
                </Form.Item>
                <Form.Item name="backend_mode_enabled" label="后台模式已开启" valuePropName="checked">
                  <Switch />
                </Form.Item>
                <Form.Item name="registration_enabled" label="自助注册已开启" valuePropName="checked">
                  <Switch />
                </Form.Item>
                <Form.Item name="email_verify_ok" label="管理员建号不受邮箱验证影响" valuePropName="checked">
                  <Switch />
                </Form.Item>
                <Space>
                  <Button type="primary" htmlType="submit" loading={save.isPending}>
                    保存
                  </Button>
                  <Button onClick={() => test.mutate()} loading={test.isPending}>
                    测试连接
                  </Button>
                </Space>
              </Form>
            ) : null}
          </Card>
        </Col>
        <Col xs={24} lg={10}>
          <Card title="中转站服务密钥">
            <Alert
              type={data?.admin_key_configured ? "success" : "warning"}
              showIcon
              message={data?.admin_key_configured ? "管理密钥已配置" : "未配置管理密钥"}
              description="至少需要 users.create、users.status.write、users.balance.read、users.balance.write 和 usage.read。没有密钥时，开通和划拨只写本地演示账本，不会请求中转站。"
            />
            <Table
              style={{ marginTop: 16 }}
              pagination={false}
              rowKey="key"
              dataSource={data?.checks ?? []}
              columns={[
                { title: "开关", dataIndex: "label" },
                {
                  title: "状态",
                  render: (_value, row) => (row.ok ? <StatusTag status="success" label={row.state} /> : <Tag color="error">{row.state}</Tag>),
                },
                { title: "说明", dataIndex: "detail" },
              ]}
            />
          </Card>
        </Col>
      </Row>
    </>
  );
}
