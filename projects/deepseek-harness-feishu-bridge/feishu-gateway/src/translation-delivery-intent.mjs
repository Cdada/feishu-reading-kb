const clean=text=>String(text??'').normalize('NFKC').trim().replace(/[。！？?!]+$/u,'');
export const isTranslationDeliveryRequest=text=>/^(?:请|麻烦)?(?:查看|看看|看|重发|再发|重新发送|发给我)(?:一下|一份)?(?:当前|这份|已有|已完成的)?(?:译稿|翻译稿|中英文对照稿|原文译文对照稿|对照版|中英对照版)$/u.test(clean(text))
  || /^(?:请)?(?:把|将)(?:这份)?(?:译稿|翻译稿)(?:改成|排成)(?:中英文对照|中英对照)(?:版|稿)?$/u.test(clean(text));
export const isRetranslationRequest=text=>/^(?:请|麻烦)?(?:重新翻译|重译)(?:一下)?(?:当前|这份)(?:材料|资料|文件)$/u.test(clean(text));
