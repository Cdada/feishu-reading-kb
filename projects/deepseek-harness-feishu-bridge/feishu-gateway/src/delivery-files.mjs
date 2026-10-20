export const extractDeliveryFiles = (value, found = []) => {
  if (!value || typeof value !== 'object') return found;
  if (typeof value.outbound_tool_path === 'string') {
    found.push({
      outboundToolPath: value.outbound_tool_path,
      fileName: value.display_name ?? value.outbound_tool_path.split('/').at(-1),
      sendable: value.feishu_sendable !== false,
    });
  }
  for (const child of Array.isArray(value) ? value : Object.values(value)) {
    extractDeliveryFiles(child, found);
  }
  return found;
};

export const extractDeliveryFilesFromText = (text, found = []) => {
  if (typeof text !== 'string' || !text.trim()) return found;
  try {
    extractDeliveryFiles(JSON.parse(text), found);
  } catch {
    // Ignore ordinary prose.
  }
  return found;
};

export const extractDeliveryFilesFromContent = (value, found = []) => {
  if (!value || typeof value !== 'object') return found;
  if (value.type === 'text' && typeof value.text === 'string') {
    extractDeliveryFilesFromText(value.text, found);
  }
  for (const child of Array.isArray(value) ? value : Object.values(value)) {
    extractDeliveryFilesFromContent(child, found);
  }
  return found;
};

export const uniqueDeliveryFiles = (files) => {
  const seen = new Set();
  return files.filter((item) => {
    if (seen.has(item.outboundToolPath)) return false;
    seen.add(item.outboundToolPath);
    return true;
  });
};
