export function getRoute() {
  const [page,key] = location.hash.slice(1).split('/');
  const section = /^\d$/.test(page) && Number(page) < 8 ? Number(page) : 0;
  const runId = /^[1-9]\d*$/.test(key || '') ? Number(key) : null;
  const month = /^\d{4}-\d{2}$/.test(key || '') ? key : null;
  return {section, runId, month};
}
