import { z } from 'zod';

export const PROVIDER_OPTIONS = [
  { label: 'Gmail', value: 'gmail' },
  { label: 'Outlook / Office 365', value: 'outlook' },
  { label: 'Yahoo Mail', value: 'yahoo' },
  { label: 'iCloud Mail', value: 'icloud' },
  { label: 'ProtonMail (Bridge)', value: 'protonmail' },
  { label: 'Fastmail', value: 'fastmail' },
  { label: 'Custom / Self-hosted', value: 'custom' },
] as const;

export const AUTH_NOTES: Record<string, string> = {
  gmail: 'Use an App Password from Google Account > Security > 2-Step Verification.',
  outlook: 'Use your account password or an App Password.',
  yahoo: 'Use an App Password from Yahoo Account Security.',
  icloud: 'Use an App-Specific Password from your Apple ID.',
  protonmail: 'Requires ProtonMail Bridge running locally (127.0.0.1).',
  fastmail: 'Use an App Password from Settings > Privacy & Security.',
  custom: 'Enter credentials for your self-hosted IMAP/SMTP server below.',
};

const PROVIDER_VALUES = PROVIDER_OPTIONS.map((p) => p.value) as [string, ...string[]];

/** Transport security modes himalaya accepts. Mirrors `_ENCRYPTIONS` in
 *  `server/nodes/email/_himalaya.py` — anything else is rejected at
 *  config-parse time. */
export const ENCRYPTION_OPTIONS = [
  { label: 'TLS / SSL', value: 'tls' },
  { label: 'STARTTLS', value: 'start-tls' },
  { label: 'None (plaintext)', value: 'none' },
] as const;

const ENCRYPTION_VALUES = ENCRYPTION_OPTIONS.map((e) => e.value) as [string, ...string[]];

/**
 * Build a zod schema for the email form. Password is required only when no
 * credential is stored yet (toggle via the `passwordRequired` flag).
 */
export function createEmailFormSchema(passwordRequired: boolean) {
  return z
    .object({
      provider: z.enum(PROVIDER_VALUES),
      address: z
        .string()
        .min(1, 'Email address is required')
        .pipe(z.email('Enter a valid email address')),
      password: passwordRequired
        ? z.string().min(1, 'Password is required')
        : z.string().optional(),
      displayName: z.string().max(200).optional(),
      imapHost: z.string().optional(),
      imapPort: z.number().int().min(1).max(65535).optional(),
      imapEncryption: z.enum(ENCRYPTION_VALUES).optional(),
      smtpHost: z.string().optional(),
      smtpPort: z.number().int().min(1).max(65535).optional(),
      smtpEncryption: z.enum(ENCRYPTION_VALUES).optional(),
    })
    .superRefine((data, ctx) => {
      if (data.provider !== 'custom') return;
      if (!data.imapHost?.trim()) {
        ctx.addIssue({
          code: 'custom',
          path: ['imapHost'],
          message: 'IMAP host is required for custom provider',
        });
      }
      if (!data.smtpHost?.trim()) {
        ctx.addIssue({
          code: 'custom',
          path: ['smtpHost'],
          message: 'SMTP host is required for custom provider',
        });
      }
      // The `custom` preset is blank server-side so these stored keys are
      // reachable; if the form omits them there is no fallback left.
      if (data.imapPort == null) {
        ctx.addIssue({
          code: 'custom',
          path: ['imapPort'],
          message: 'IMAP port is required for custom provider',
        });
      }
      if (data.smtpPort == null) {
        ctx.addIssue({
          code: 'custom',
          path: ['smtpPort'],
          message: 'SMTP port is required for custom provider',
        });
      }
    });
}

export type EmailFormValues = z.infer<ReturnType<typeof createEmailFormSchema>>;
