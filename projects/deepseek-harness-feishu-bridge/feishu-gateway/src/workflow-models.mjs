// User-approved profiles, scoped to Feishu work; never change desktop defaults.
export const translationModel = Object.freeze({model:'gpt-5.6-sol', effort:'high'});
export const curationModel = Object.freeze({model:'gpt-6-astra', effort:'medium'});
export const directTranslationModel = Object.freeze({model:'gpt-6-astra',effort:'medium'});
export const dshReasoningEffort = 'high';
export const dshTranslationModel = Object.freeze({
  provider:'deepseek-official', model:'deepseek-flash', reasoningEffort:dshReasoningEffort,
});
