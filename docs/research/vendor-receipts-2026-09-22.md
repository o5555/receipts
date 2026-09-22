# Vendor receipts: stable collection methods

Research for [issue 8](https://github.com/o5555/receipts/issues/8), completed 2026-09-22. Question: for each vendor that lacks a receipt in August 2026 (Kinto, Starlink, SATS, NordVPN, Anthropic Claude Pro, OpenAI ChatGPT, Kivra), what is the stable way to get the receipt without Oscar forwarding anything by hand?

Every claim below is taken from the vendor's own help centre, terms or developer documentation, read on 2026-09-22. "Not found" means the vendor does not document it; it is not a claim that the feature is absent. Local knowledge is what the receipts repo and OBrain already record about the account in question. Logins, account ids and mailbox ids are deliberately left out; they stay in local storage.

Method vocabulary:

- **Billing email**: the vendor sends the receipt to an address the receipts system already reads. Zero manual work once configured.
- **Portal, Oscar downloads**: a self-service page exists, but the login (BankID, magic link, one-time code) needs Oscar present.
- **Portal, delegable**: the page needs only a password; a delegated person or a scripted browser can fetch without Oscar. Only a fallback; portal scraping is explicitly out of scope in Kvittoplanen.
- **API**: a machine interface for invoices.
- **No known method**.

## Summary

| Vendor | Emails receipts | Portal with PDFs | Login | Fetch without Oscar | Recommended method |
| --- | --- | --- | --- | --- | --- |
| Kinto | yes, per booking | yes | password | yes | billing email |
| Starlink | yes, monthly statement | yes | password + email code | no | billing email |
| SATS | not found | yes | password | yes (terms forbid sharing) | portal, Oscar downloads |
| NordVPN | no | yes, on request | password, optional MFA | yes if MFA off | portal, Oscar downloads |
| Anthropic | yes, per charge | yes | Google or email link | no | billing email |
| OpenAI | not found | yes | password or SSO, risk-based code | partly | billing email |
| Kivra | no | yes | BankID | no | portal, Oscar downloads |

## Kinto (KINTO Share, Toyota Sweden AB)

- **Emails receipts**: yes. After every finished booking a summary of all charges is emailed to the account address; a booking produces at least two receipts (time charge at start, kilometre charge at end), an extension adds one more. Receipts are also in the app under Mina bokningar > Avslutade and as PDF under Mitt KINTO > bokningar > avslutade. [FAQ 2649](https://faq.kinto-mobility.se/category/217/article/2649), [terms 10.1 and 13](https://www.kinto-mobility.se/kinto-share/allmanna-villkor/).
- **Which address**: the account email, which is also the login. Changing it after a booking is not self-service; Kinto support has to do it. [FAQ 2659](https://faq.kinto-mobility.se/category/218/article/2659).
- **Portal**: yes, [Mitt KINTO](https://www.kinto-mobility.se/mitt-kinto/); PDF download per finished booking. [FAQ 2610](https://faq.kinto-mobility.se/article/2610). A separate business product with an admin portal and per-employee invoicing exists. [Så funkar det för företag](https://www.kinto-mobility.se/kinto-share/foretag/sa-funkar-det-for-foretag/).
- **Login**: email + password. No BankID or SMS step in the FAQ, terms or app listing. [FAQ 2661](https://faq.kinto-mobility.se/category/218/article/2661).
- **API**: none found; the backend is Ridecell, no public member API.
- **Monthly plan**: the 499 SEK charge on the 8th is the Plus membership, billed monthly in advance. [Priser](https://www.kinto-mobility.se/kinto-share/priser/), terms 7.1.3. Whether the membership fee gets its own emailed or downloadable receipt is not documented.
- **Local knowledge**: Kinto has emailed full per-booking receipts to Oscar's personal Gmail since April 2026, and that mailbox is aggregated into the viseo.se mailbox the receipts system reads. Ten Kinto receipts were recovered from the July pack that way. Whether the 499 SEK plan is business is still Oscar's call (OBrain, open questions).
- **Conclusion**: (a) emails automatically: yes, per booking. (b) portal: yes. (c) login: password. (d) delegable: yes, with a shared login.
- **Recommended method**: billing email. The per-booking emails already reach the mailbox; the Gmail matcher only needs to accept several receipts per booking. For the 499 SEK plan fee: check the next email on the 8th; if nothing arrives, Mitt KINTO is the delegable fallback.

## Starlink

- **Emails receipts**: yes. Monthly statements are emailed automatically to the account's main user(s), plus an email per transaction and a "Payment Scheduled" email. Whether the statement email carries the PDF or only a notice is not stated. [Invoices and statements](https://starlink.com/support/article/a274f8db-1791-8676-c265-af3637cafa9e), [transaction emails](https://starlink.com/support/article/d84788ba-a01c-404d-f0bf-582db722ac79).
- **Billing email**: no separate one. The account email is changed under Settings > Edit Profile and is login and communication address at once. [Change email](https://starlink.com/support/article/c194acc6-d6df-86db-1d1b-4841450086c6). Workaround: add an extra user (Residential up to 3, Business up to 500); each user gets the statement. [Add users](https://starlink.com/support/article/b33da5ba-46f4-c93a-5cbb-700edae91188).
- **Portal**: yes, starlink.com/account > Billing > Statements, PDF download, no retention limit stated. [Download statements](https://starlink.com/support/article/c6a68e60-b6c2-e55b-5625-89b9d9ddcf14).
- **Login**: email + password and mandatory two-step verification with a passcode sent to the account email (SMS fallback); no authenticator app. [Two-step verification](https://starlink.com/support/article/52aff4ed-3167-ec24-d54c-249563df8f5e).
- **API**: Enterprise only. API v2 has `GET /public/v2/billing/invoices` (JSON, no PDF field) behind a service account with the "Financial, View" permission. [Reference](https://starlink.readme.io/reference/get_public-v2-billing-invoices), [changelog 2026-07-08](https://starlink.readme.io/changelog/new-invoices-and-account-balance-api-endpoints).
- **Company details**: only Business accounts carry company information; a Residential account cannot add it and would need a Business account plus a hardware transfer. [Business information](https://starlink.com/support/article/9334ff65-c6e4-00d3-23ca-4ac4007c88fa).
- **Local knowledge**: Starlink is in `scripts/merchant_rules.json` with a kontering rule; nothing in the journals says the statement email has been seen in the mailbox.
- **Conclusion**: (a) emails automatically: yes, monthly statement to the account users. (b) portal: yes, PDF. (c) login: password plus email passcode. (d) delegable: no, the passcode goes to the account email on every sign-in.
- **Recommended method**: billing email. Either the account email is a viseo.se address, or a second user with a viseo.se address is added so the monthly statement lands in the mailbox the system reads. If the email turns out to be a notice without PDF, the fallback is portal, Oscar downloads. If Trimero wants Viseo AB on the invoice, the account has to become a Business account.

## SATS

- **Emails receipts**: not found. The help pages only describe self-service download. [Betalningshistorik](https://www.sats.se/kundservice/betalning/mina-betalningar/betalingshistorik), [medlemsvillkor](https://www.sats.se/legal/almanna-villkor-for-medlemskap-i-sats).
- **Portal**: yes. Min sida > Betalningsöversikt: PDF receipt per payment and a receipt for the whole year; a payment can take up to three working days to show. Same in the app under Aktivitet > Mitt medlemskap > Betalningshistorik. Entry [sats.se/mina-sidor](https://www.sats.se/mina-sidor/), which lands on min.sats.se.
- **Login**: email or member number + password. No BankID or SMS. [Inloggning](https://www.sats.se/kundservice/min-profil/innloggning/mina-sidor). Terms 4.4 forbid sharing the credentials.
- **Charging**: due on the 28th, autogiro or card set on Min sida. [Betala faktura](https://www.sats.se/kundservice/betalning/mina-betalningar/pay-invoice).
- **Company details on the receipt**: not found. Employer-benefit portals (Epassi, Benifex and others) exist for friskvård, not a company invoice.
- **API**: none found.
- **Local knowledge**: SATS sends no renewal receipts to the mailbox (OBrain, 2026-08-21); the open rows are 753 SEK on 28 July and 28 August. Kvittoplanen already proposes the annual receipt once a year.
- **Conclusion**: (a) emails automatically: not found. (b) portal: yes, PDF per payment and per year. (c) login: password. (d) delegable: technically yes, but the terms forbid sharing the login.
- **Recommended method**: portal, Oscar downloads. Once a year the annual receipt, or the monthly PDF after the 28th plus three days. A delegated fetch with a shared password is possible but goes against SATS terms, so it stays a conscious choice by Oscar.

## NordVPN

- **Emails receipts**: no. Receipts and invoices are generated on request in Nord Account > Billing > Billing history > Get invoice; only the first purchase gets a confirmation email. [Terms of service](https://my.nordaccount.com/legal/terms-of-service/), [How can I get a receipt or an invoice](https://support.nordvpn.com/hc/en-us/articles/20371782137873-How-can-I-get-a-receipt-or-an-invoice-for-my-payment).
- **Portal**: yes, [my.nordaccount.com](https://my.nordaccount.com) > Billing > Billing history > Get invoice, with a form for name, company, address and VAT number before download. [Invoice form](https://support.nordvpn.com/hc/en-us/articles/19744005126801).
- **Billing email**: no separate billing email; the registration email can be changed with a code to the old address and a link to the new one. [Change email](https://support.nordvpn.com/hc/en-us/articles/19482546152977).
- **Login**: email + password or Google/Apple; MFA optional (TOTP app or FIDO2 key, backup codes). [MFA](https://support.nordvpn.com/hc/en-us/articles/19442299167889).
- **API**: none found.
- **Local knowledge**: NordVPN sends no renewal receipts (OBrain, 2026-08-21); open rows 21 July and 21 August. Kvittoplanen proposes a two-year plan so this happens once every two years.
- **Conclusion**: (a) emails automatically: no. (b) portal: yes, invoice generated on request with company and VAT fields. (c) login: password, optional MFA. (d) delegable: yes if MFA is off and the login is shared; no multi-user access.
- **Recommended method**: portal, Oscar downloads. Generate the invoice with Viseo AB and the VAT number at each renewal; move to a two-year plan so it is one download every two years. A delegable fetch is possible but not worth building for that frequency.

## Anthropic (Claude Pro / Max on claude.ai)

- **Emails receipts**: yes. "After each charge, we automatically email the invoice to your billing email address", subject "Your receipt from Anthropic". [Pro or Max invoices](https://support.claude.com/en/articles/16607638-understanding-your-pro-or-max-plan-invoices), [billing FAQ](https://support.claude.com/en/articles/8325618-paid-plan-billing-faqs). Which address is the "billing email" for a Pro/Max account, and whether it can differ from the login, is not documented; only the Team plan documents changing it, via support. Mobile-app subscriptions are billed and receipted by the App Store or Google Play.
- **Portal**: yes, claude.ai Settings > Billing > Invoices > View.
- **Company details**: "Use a different name on invoices" and a tax or VAT ID can be set when updating the payment method; applies to future invoices only. [Tax or VAT ID](https://support.claude.com/en/articles/9889408-add-or-update-your-paid-claude-account-s-tax-or-vat-id).
- **Login**: Google or an emailed login link (code on another device). No password, no MFA for personal accounts. [Log in](https://support.claude.com/en/articles/13189465-log-in-to-your-claude-account).
- **API**: none for consumer invoices. Console (API) billing emails a receipt after each charge too, with history under Console Settings > Billing. [API invoices](https://support.claude.com/en/articles/16608069-understanding-your-claude-api-invoices).
- **Local knowledge**: the Claude Pro subscription (13 June, 13 July, 13 August) is billed to a login none of Oscar's mailboxes sees, so the receipts do arrive but in an unread inbox. The Claude Max subscription on the Pleo card was fetched from Gmail and attached on 2026-08-24, which confirms the email path works when the login mailbox is readable.
- **Conclusion**: (a) emails automatically: yes, per charge. (b) portal: yes. (c) login: Google or email link, no password. (d) delegable: no, every login needs the login inbox or the Google session.
- **Recommended method**: billing email. Oscar identifies the login behind the Pro subscription once; then either the receipts already land in a readable mailbox, or that inbox forwards "Your receipt from Anthropic" to the viseo.se mailbox. Set the invoice name to Viseo AB and the VAT number at the same time. No portal automation is possible with a magic-link login.

## OpenAI (ChatGPT Plus / Pro)

- **Emails receipts**: not found for web subscriptions. OpenAI states that "the email where you receive invoices or receipts may be different from your ChatGPT account email", which implies receipts are sent but does not promise one per renewal. [What is ChatGPT Plus](https://help.openai.com/en/articles/6950777-what-is-chatgpt-plus), [billing information](https://help.openai.com/en/articles/9038389-updating-billing-information-tax-id-and-vat-id).
- **Billing email**: separate from the login and editable under Settings > Billing > Billing information, or via Settings > Account > Payment > Manage > Billing information. Same article. Tax ID can be added; changes apply to future invoices, and an old invoice can be reissued by support.
- **Portal**: yes, Settings > Account > Payment > Manage > Invoice History (newer accounts: Settings > Billing). [Past invoices](https://help.openai.com/en/articles/12356340-how-can-i-find-my-past-chatgpt-invoices). App Store and Google Play subscriptions do not appear there; Apple emails its own receipt to the Apple ID address. [Apple receipts](https://help.openai.com/en/articles/9030143-how-do-i-obtain-my-chatgpt-subscription-invoice-if-i-subscribed-from-the-apple-app-store).
- **Login**: password or Google/Microsoft/Apple SSO. MFA optional. Even without MFA, a new device or location can trigger an email code or app push. [Login verification](https://help.openai.com/en/articles/9889414-why-am-i-being-asked-to-verify-my-login).
- **API**: none for consumer invoices.
- **Local knowledge**: Kvittoplanen states that OpenAI never emails receipts; the primary sources neither confirm nor deny that. The August ChatGPT row (7 August) and an earlier OpenAI receipt made out to 5555 Media AB show the account is not yet set up for Viseo AB.
- **Conclusion**: (a) emails automatically: not documented. (b) portal: yes, Invoice History. (c) login: password or SSO with risk-based verification. (d) delegable: partly; a scripted login from a new device will likely trigger a code to Oscar.
- **Recommended method**: billing email. Set the billing email to the viseo.se mailbox and the tax ID to Viseo AB in Settings > Billing, then check whether the next renewal produces an email. If it does not, the fallback is portal, Oscar downloads, one PDF per month. First confirm the subscription is billed by OpenAI and not through the App Store, since app-store receipts come from Apple instead.

## Kivra (Kivra Företag)

- **The fee**: 123,75 SEK is Kivra Företag Plus, 99 SEK per month excluding VAT, charged in arrears. [Kivra Företag Plus](https://faq.kivra.se/hc/sv/articles/22859000765213), [business terms](https://kivra.se/en/about-kivra/terms/general-terms-and-conditions-for-business-users).
- **Emails receipts**: no. The terms point inside the mailbox; nothing says the fee receipt is emailed. The company mailbox email only drives notifications. [Notifications](https://faq.kivra.se/hc/sv/articles/22962443091357).
- **Portal**: the company mailbox, reached from the signatory's private Kivra. Where exactly the fee invoice is placed is not documented. [Company mailbox](https://faq.kivra.se/hc/sv/articles/22963902266909).
- **Login**: Mobile BankID only. [Log in](https://faq.kivra.se/hc/sv/articles/34085009170205).
- **Delegation**: a signatory can share the company mailbox (read, or read and write) with a person who has their own Kivra and Mobile BankID. [Share access](https://faq.kivra.se/hc/sv/articles/30119042271901).
- **API**: developer APIs are for senders only. The documented pull path is the accounting-system integration (Fortnox among others), activated while logged in, which gives the accounting system access to all mailbox content. Whether Kivra's own fee invoice flows through it is not documented. [Integrations](https://faq.kivra.se/hc/sv/articles/22961400508317).
- **Local knowledge**: Nicolina wants a receipt for every Kivra fee; nothing Kivra-related reaches the Fortnox inbox today (OBrain, 2026-08-21). Open rows: 20 April through 31 August, seven fees.
- **Conclusion**: (a) emails automatically: no. (b) portal: yes, behind BankID. (c) login: BankID. (d) delegable: only by another person with their own BankID and shared access; no script.
- **Recommended method**: portal, Oscar downloads. Oscar checks once whether Kivra places its own invoice in the company mailbox; if yes, activating the Kivra to Fortnox integration would let Nicolina book it from the Fortnox inbox without downloads. Otherwise a quarterly BankID session for the PDFs, as Kvittoplanen already says.

## What this changes

- Three of the seven (Kinto, Anthropic, Starlink) email receipts today; the gap is which mailbox receives them. Fixing the address is a one-time task for Oscar, not a fetch problem.
- OpenAI is the one vendor with a proper billing-email field separate from the login; one setting may close it.
- SATS, NordVPN and Kivra never email. Kivra is BankID-only. SATS and NordVPN are password portals, so a delegated fetch is possible in principle, but their low frequency (yearly, every two years) and the SATS terms make "Oscar downloads" the sane default.
- No vendor offers a usable invoice API for this account type; Starlink's is Enterprise-only and JSON-only.
- Portal scraping stays out of scope, as Kvittoplanen says. The next steps are Oscar's vendor pass (Kvittoplanen step 6): fix the Anthropic login mailbox, set the OpenAI billing email and tax ID, put a viseo.se user on Starlink, and register Viseo AB and the VAT number in every portal that takes it.
