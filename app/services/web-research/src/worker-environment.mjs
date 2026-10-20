export function workerEnvironment(source = process.env, extraNames = []) {
  const allowed = new Set(['SYSTEMROOT','WINDIR','PATH','PATHEXT','COMSPEC','SYSTEMDRIVE',
    'PROGRAMFILES','PROGRAMFILES(X86)','LOCALAPPDATA','TEMP','TMP','TMPDIR','LANG','LC_ALL',
    ...extraNames.map(name=>name.toUpperCase())]);
  return { ...Object.fromEntries(Object.entries(source).filter(([key])=>allowed.has(key.toUpperCase()))),
    PYTHON_DOTENV_DISABLED: '1' };
}
