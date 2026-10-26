export const ASSISTANT_NAME = 'Solevia'
export const ASSISTANT_CHINESE_NAME = '足下'
export const ASSISTANT_MOTTO = '千里之行，始于足下'

// Read old assistant replies without rewriting the user's saved conversations.
export function normalizeAssistantName(content: string) {
  return content.replace(/Plan-A/g, ASSISTANT_NAME)
}
