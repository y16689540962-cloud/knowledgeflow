/* URL 判定 —— 与后端 ``app/ingestion/redirects.py`` 的 ``extract_aweme_id`` 同口径。
 *
 * 为什么单独一个文件：判定逻辑要**能被测试真的跑一遍**（用 node 加载它），
 * 塞在 popup.js 里就只能靠人点。口径还必须和后端一致 ——
 * 扩展说「能采」而后端说「解析不出 aweme_id」，那是最难受的错位
 * （用户会看到一个自己完全没预料到的 502）。
 */
(function (root) {
  "use strict";

  var DOUYIN_HOSTS = ["douyin.com", "iesdouyin.com"];

  /* 与后端 AWEME_ID_PATTERNS 一致：都要 6 位以上数字，避免把 /video/list 当成 id。 */
  var AWEME_PATTERNS = [
    /\/(?:video|note|slides|share\/video|share\/note)\/(\d{6,})/,
    /[?&]modal_id=(\d{6,})/,
    /[?&]aweme_id=(\d{6,})/
  ];

  function hostOf(url) {
    try {
      return new URL(url).hostname.toLowerCase();
    } catch (e) {
      return "";
    }
  }

  function isDouyinHost(url) {
    var host = hostOf(url);
    return DOUYIN_HOSTS.some(function (suffix) {
      return host === suffix || host.endsWith("." + suffix);
    });
  }

  function awemeIdOf(url) {
    if (!url) return null;
    for (var i = 0; i < AWEME_PATTERNS.length; i++) {
      var m = AWEME_PATTERNS[i].exec(url);
      if (m) return m[1];
    }
    return null;
  }

  /* 三态判定：能采 / 是抖音但没有视频 id / 根本不是抖音。
   * 没有 id 时**不许**去猜首页推荐流里在放哪一条 ——
   * 实测抖音首页对新环境直接给「验证码中间页」，DOM 里一条视频链接都没有，
   * 猜出来的 id 大概率是错的，而用户看不出它是错的。 */
  function describe(url) {
    if (!isDouyinHost(url)) {
      return { kind: "other", text: "当前页不是抖音 —— 请打开抖音视频页再点。" };
    }
    var id = awemeIdOf(url);
    if (id) {
      return { kind: "ok", text: "识别到视频 " + id, id: id };
    }
    return {
      kind: "noid",
      text: "这是抖音的首页 / 频道页，地址里没有视频 id。" +
        "请点开具体视频（地址栏会变成含 /video/数字 的样子），" +
        "或者用抖音的「复制链接」把链接粘到下面。"
    };
  }

  root.KFUrlMatch = {
    hostOf: hostOf,
    isDouyinHost: isDouyinHost,
    awemeIdOf: awemeIdOf,
    describe: describe
  };
})(typeof globalThis !== "undefined" ? globalThis : this);
