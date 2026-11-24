export const roles = {analyst: '分析员', reviewer: '复核员', viewer: '只读访客'};
export const states = {queued: '排队', running: '计算中', completed: '已计算', failed: '需修正'};
export const decisions = {approved: '已批准', returned: '已退回'};
export const actions = {
  import: '导入数据', queue: '提交计算', completed: '完成计算', failed: '计算失败',
  ready: '重编完成待复核', approved: '批准发布', returned: '退回结果',
  schedule: '建立计划', cancel: '停用计划', delivered: '报告入箱',
  amendment: '登记修订', reissue: '发起重编', retry: '重试重编',
};
export const reissueStates = {
  queued: '等待重算', running: '重算中', ready: '待复核', failed: '重算失败',
  returned: '已退回', approved: '已切换发布',
};
export const reissueRunStates = {
  queued: '等待重算', running: '重算中', completed: '已重算', failed: '失败',
};
export const amendmentActions = {'改期': '改期', '撤销': '撤销'};
