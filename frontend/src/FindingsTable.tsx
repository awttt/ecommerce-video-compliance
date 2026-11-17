import { Segmented, Table, Tooltip } from "antd";
import type { ColumnsType } from "antd/es/table";
import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";

import {
  applyPageChange,
  CATEGORY_FILTERS,
  clampPage,
  DEFAULT_PAGE_SIZE,
  FILTER_ALL,
  filterFindings,
  findingKey,
  findingsEmptyText,
  lastMatchingRow,
  locateFinding,
  PAGE_SIZE_OPTIONS,
  selectCategory,
} from "./findingsView";
import type { Finding } from "./types";

const CATEGORY_PILL: Record<string, string> = {
  价格: "pill--price",
  规格: "pill--spec",
  品牌: "pill--brand",
  夸大宣传: "pill--hype",
};

export interface FindingsTableHandle {
  reveal: (finding: Finding) => void;
}

interface FindingsTableProps {
  jobId: string;
  findings: readonly Finding[];
  running: boolean;
  activeKey: string;
  onOpen: (finding: Finding) => void;
}

const columns: ColumnsType<Finding> = [
  { title: "时间点", dataIndex: "时间点", width: 130 },
  {
    title: "类别",
    dataIndex: "类别",
    width: 80,
    render: (value: string) => <span className={`pill ${CATEGORY_PILL[value] ?? "pill--neutral"}`}>{value}</span>,
  },
  {
    title: "来源",
    dataIndex: "来源",
    width: 80,
    render: (value: string) => <span className={`pill ${value === "口播" ? "pill--speech" : "pill--neutral"}`}>{value || "画面"}</span>,
  },
  { title: "原文", dataIndex: "原文", width: 160 },
  { title: "位置", dataIndex: "位置", width: 180 },
  {
    title: "置信度",
    dataIndex: "置信度",
    width: 90,
    render: (value: string) => <span className={`pill ${value === "确定" ? "pill--confirmed" : "pill--pending"}`}>{value}</span>,
  },
  {
    title: "判定理由",
    dataIndex: "判定理由",
    ellipsis: { showTitle: false },
    render: (value: string) => (
      <Tooltip title={value}>
        <span>{value}</span>
      </Tooltip>
    ),
  },
  { title: "备注", dataIndex: "备注", width: 160, ellipsis: true },
];

export const FindingsTable = forwardRef<FindingsTableHandle, FindingsTableProps>(function FindingsTable(
  { jobId, findings, running, activeKey, onOpen },
  ref,
) {
  const [categoryFilter, setCategoryFilter] = useState(FILTER_ALL);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [trackedJobId, setTrackedJobId] = useState(jobId);
  const [scrollRequest, setScrollRequest] = useState<{ key: string; nonce: number } | null>(null);
  const tableRef = useRef<HTMLDivElement>(null);
  const scrollNonce = useRef(0);

  const jobChanged = trackedJobId !== jobId;
  const activeFilter = jobChanged ? FILTER_ALL : categoryFilter;
  const requestedPage = jobChanged ? 1 : page;
  const visibleFindings = filterFindings(findings, activeFilter);
  const safePage = clampPage(requestedPage, visibleFindings.length, pageSize);
  if (jobChanged) {
    setTrackedJobId(jobId);
    if (categoryFilter !== FILTER_ALL) setCategoryFilter(FILTER_ALL);
    if (scrollRequest !== null) setScrollRequest(null);
  }
  if (safePage !== page) setPage(safePage);

  useEffect(() => {
    if (!scrollRequest) return undefined;
    let cancelled = false;
    const run = () => {
      if (cancelled || !tableRef.current) return;
      const row = lastMatchingRow(tableRef.current.querySelectorAll("tr[data-row-key]"), scrollRequest.key);
      row?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    };
    run();
    const frame = window.requestAnimationFrame(run);
    return () => {
      cancelled = true;
      window.cancelAnimationFrame(frame);
    };
  }, [scrollRequest]);

  const snapshot = useRef({ findings, filter: activeFilter, pageSize, onOpen });
  snapshot.current = { findings, filter: activeFilter, pageSize, onOpen };

  useImperativeHandle(
    ref,
    () => ({
      reveal(finding: Finding) {
        const current = snapshot.current;
        const located = locateFinding(current.findings, finding, current.filter, current.pageSize);
        if (located.clearedFilter) setCategoryFilter(located.filter);
        setPage(located.page);
        scrollNonce.current += 1;
        setScrollRequest({ key: findingKey(finding), nonce: scrollNonce.current });
        current.onOpen(finding);
      },
    }),
    [],
  );

  return (
    <>
      <div className="findings-toolbar" role="group" aria-label="按问题类别筛选">
        <span className="findings-filter-label">问题类别</span>
        <Segmented
          value={activeFilter}
          options={CATEGORY_FILTERS.map((item) => ({ label: item, value: item }))}
          onChange={(value) => {
            const next = selectCategory(String(value));
            setCategoryFilter(next.filter);
            setPage(next.page);
          }}
        />
      </div>
      <div className="findings-table" ref={tableRef}>
        <Table
          size="small"
          rowKey={(record) => findingKey(record)}
          columns={columns}
          dataSource={visibleFindings as Finding[]}
          pagination={
            visibleFindings.length === 0
              ? false
              : {
                  // pageSize 放在 state 里。写死后选择器改条数不会进入下一次渲染。
                  current: safePage,
                  pageSize,
                  total: visibleFindings.length,
                  showSizeChanger: true,
                  pageSizeOptions: PAGE_SIZE_OPTIONS.map(String),
                  hideOnSinglePage: false,
                  showTotal: (total) => `共 ${total} 条`,
                  onChange: (nextPage, nextSize) => {
                    const next = applyPageChange(nextPage, nextSize, visibleFindings.length);
                    setPage(next.page);
                    setPageSize(next.pageSize);
                  },
                }
          }
          rowClassName={(record) => (findingKey(record) === activeKey ? "click-row finding-row-active" : "click-row")}
          onRow={(record) => ({ onClick: () => onOpen(record) })}
          locale={{ emptyText: findingsEmptyText(running, activeFilter) }}
        />
      </div>
    </>
  );
});
