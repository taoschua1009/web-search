# User-Supplied Customer Email Samples

Source: the user's attached `Screenshot 2026-10-25 at 8.42.31 PM.png`, received 2026-10-25. The actual attachment filename uses a narrow nonbreaking space before PM. This is input provenance, not evidence that any email was received or that a company fact has been researched.

Canonical data: `src/services/research/fixtures/customer_emails.json`.

| Sender in the sample | Company | Website provided | Message content |
|---|---|---|---|
| Nguyễn Văn A | FPT Software | https://fptsoftware.com | Chúng tôi muốn tìm hiểu giải pháp AI cho doanh nghiệp. |
| Trần Minh Đức | Vinamilk | https://www.vinamilk.com.vn | Mong muốn trao đổi về ứng dụng AI trong quản lý tài liệu. |
| Sarah Lee | Samsung Electronics Vietnam | https://www.samsung.com/vn/ | Xin hẹn buổi giới thiệu giải pháp vào tuần tới. |
| David Chen | Shopee Vietnam | https://shopee.vn | Chúng tôi quan tâm đến giải pháp OCR. |
| Nguyễn Hoàng Anh | Viettel Solutions | https://viettelsolutions.vn | Đính kèm hồ sơ năng lực để hai bên tham khảo. |
| Emily Wong | Bosch Global Software Technologies Vietnam | https://www.bosch.com/vn | Chúng tôi muốn trao đổi về giải pháp AI Automation. |

## What is not supplied

The image gives no actual sender/recipient email addresses, subject headers, sent dates, or files. Generated `@example.invalid` addresses, IDs, thread references, and display subjects exist only to exercise the application UI. They are not inferred contact details.

Samsung gives no exact date/time, timezone, or duration. Its meeting stays `needs_clarification`; a simulated next-week availability window does not become a claimed appointment. Viettel mentions an attachment, but no file is supplied, so the report explicitly states it cannot analyze that profile.

## Demo behavior

New demo login defaults to these six inputs. They produce separate input-only company reports using source type `sample_email` and user-provided provenance. Unread company URLs are preserved as input text, not presented as read/verified sources. Facts about company scale, products, pricing, people, news, and adverse events remain missing until real research is performed through a suitable live workflow. The sample dataset itself performs no network or paid-model requests.

The original synthetic dataset remains selectable in the inbox to exercise invented company profiles, adverse scenarios, conflict/already-scheduled/available calendars, and approved simulated sends/Trash. These scenarios are not transferred onto the real companies in the user's examples. Switching datasets creates a separate demo identity to avoid mixing reports or memory.

The same live workflow continues to handle arbitrary companies from the signed-in user's actual selected emails; the six example names are not a production whitelist.
