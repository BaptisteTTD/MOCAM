/** Langue de l'app : français si le téléphone est en français, anglais sinon. */
function deviceLocale(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().locale || 'en'
  } catch {
    return 'en'
  }
}

export const LANG: 'fr' | 'en' = deviceLocale().toLowerCase().startsWith('fr') ? 'fr' : 'en'

/** Texte dans la langue du téléphone. */
export function t(fr: string, en: string): string {
  return LANG === 'fr' ? fr : en
}

/** Messages envoyés par MOCAM Studio (écrits en français). */
const STUDIO_MESSAGES: Record<string, string> = {
  'un enregistrement est déjà en cours': 'a recording is already in progress',
  'QR code périmé : rescannez-le': 'QR code expired: scan it again',
}

export function tStudio(msg: string): string {
  return LANG === 'fr' ? msg : STUDIO_MESSAGES[msg] ?? msg
}

/** Nombre avec la virgule (français) ou le point (anglais). */
export function num(v: number): string {
  return LANG === 'fr' ? String(v).replace('.', ',') : String(v)
}
