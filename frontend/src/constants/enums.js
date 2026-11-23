export const Role = Object.freeze({ VIEWER: 'viewer', ANALYST: 'analyst', SUPERVISOR: 'supervisor' });
export const RunStatus = Object.freeze({ QUEUED: 'queued', SUCCEEDED: 'succeeded', FAILED: 'failed' });
export const Decision = Object.freeze({ APPROVED: 'approved', RETURNED: 'returned' });
export const VoucherStatus = Object.freeze({ QUEUED: 'queued', POSTED: 'posted', WAITING: 'waiting', FAILED: 'failed' });
export const roleName = {viewer: '只读查看', analyst: '业务分析员', supervisor: '复核负责人'};
export const statusName = {queued: '等待计算', succeeded: '等待复核', failed: '计算失败', approved: '已签核', returned: '已退回',
  posted: '已入账', waiting: '余额不足·挂起', debit: '补入（借）', credit: '扣回（贷）'};
