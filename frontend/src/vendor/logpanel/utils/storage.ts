/**
 * 带前缀的 localStorage 读写。
 *
 * 持久化是增强能力：读写失败（隐私模式、配额、历史脏数据）一律回退默认值，不中断面板。
 */

export interface PanelStorage {
  readJSON<T>(key: string, fallback: T): T;
  writeJSON(key: string, value: unknown): void;
  readBool(key: string, fallback: boolean): boolean;
  writeBool(key: string, value: boolean): void;
}

export function createStorage(prefix: string): PanelStorage {
  return {
    readJSON<T>(key: string, fallback: T): T {
      try {
        const raw = localStorage.getItem(prefix + key);
        return raw === null ? fallback : (JSON.parse(raw) as T);
      } catch {
        return fallback;
      }
    },
    writeJSON(key: string, value: unknown): void {
      try {
        localStorage.setItem(prefix + key, JSON.stringify(value));
      } catch {
        /* 配额/隐私模式：忽略 */
      }
    },
    readBool(key: string, fallback: boolean): boolean {
      try {
        const raw = localStorage.getItem(prefix + key);
        return raw === null ? fallback : raw === 'true';
      } catch {
        return fallback;
      }
    },
    writeBool(key: string, value: boolean): void {
      try {
        localStorage.setItem(prefix + key, String(value));
      } catch {
        /* 同上 */
      }
    },
  };
}