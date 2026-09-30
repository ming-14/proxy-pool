/**
 * 页面写动作骨架：请求 → 成功/失败 toast → 收尾（刷新/清选等）。
 *
 * 错误文案统一走 errText（与 api/client 同一入口）；异常不外抛，动作之间不会
 * 因一个失败影响后续逻辑。成功提示可按响应自定义（如"已有任务在跑"改走 info）。
 */
import { errText } from '@/api/client';
import { useToastStore, type ToastType } from '@/stores/toast';

/** toast 内容（type 缺省 success） */
export interface ActionToast {
  type?: ToastType;
  title: string;
  message?: string;
}

export interface ActionOptions<T> {
  /** 成功提示；给函数则按返回值生成 */
  ok: ActionToast | ((result: T) => ActionToast);
  /** 失败提示标题（正文取错误信息） */
  failTitle: string;
  /** 收尾：成功失败都执行（刷新状态/复位按钮/清空选中） */
  after?: () => void | Promise<void>;
}

export async function runAction<T>(
  action: () => Promise<T>,
  opts: ActionOptions<T>,
): Promise<T | undefined> {
  const toast = useToastStore();
  try {
    const result = await action();
    const t = typeof opts.ok === 'function' ? opts.ok(result) : opts.ok;
    toast.show(t.type ?? 'success', t.title, t.message);
    return result;
  } catch (e) {
    toast.error(opts.failTitle, errText(e));
    return undefined;
  } finally {
    try {
      await opts.after?.();
    } catch (e) {
      // 收尾（刷新/复位）属尽力而为：失败静默，不能外抛成未处理 rejection
      console.warn('runAction after failed', e);
    }
  }
}
