'use strict';

/**
 * 托盘/窗口图标。
 *
 * 用户暂不做自定义图标，但托盘在 Windows 上必须有一张有效图像才可见可点
 * （传入空图会得到一个不可见、点不到的占位），因此这里内联一张 16x16 PNG：
 * 圆环 + 中心点的「网络节点」图形，纯色 #2563EB（明暗主题下都可见）。
 */

const TRAY_ICON_PNG_BASE64 =
  'iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAiUlEQVR42q2TsQ0AIQhFLVyFEZzCMSivcLjr2MaaOMNdg8kP0VwuWPwG+U9ATMSaIloFM7E2YhVi7SaxWP4CVDM8G3XLWQLqwjBMPl49ILubb2ItAC8Ww0oyApoz74aGkIYAgQO8+TJhJTNPENChZzTPZIQMaOMcINxCeIjhZwwv0pFVPvKZfusFkT/apMorHT8AAAAASUVORK5CYII=';

module.exports = { TRAY_ICON_PNG_BASE64 };