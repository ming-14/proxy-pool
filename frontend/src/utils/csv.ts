/** CSV 构建与浏览器端下载（纯函数，可单测） */

export function csvEscape(value: unknown): string {
  return `"${String(value ?? '').replace(/"/g, '""')}"`;
}

export function buildCsv(header: string[], rows: unknown[][]): string {
  const lines = rows.map((r) => r.map(csvEscape).join(','));
  return [header.join(','), ...lines].join('\n');
}

export function downloadFile(content: string, filename: string, mime: string): void {
  const url = URL.createObjectURL(new Blob([content], { type: mime }));
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}
