"use strict";
const $ = (id) => document.getElementById(id);
const state = {
  config: null,
  messages: [],
  batches: [],
  actions: [],
  selected: new Set(),
  batch: null,
  poll: null,
  report: null,
};
const labels = {
  email: "Email",
  web_research: "Web Research",
  company_info: "Company Info",
  calendar: "Calendar",
  report_generation: "Report",
  memory: "Memory",
  human_approval: "Human Approval",
  overview: "Tổng quan",
  industry: "Lĩnh vực",
  products: "Sản phẩm & dịch vụ",
  scale: "Quy mô",
  contacts: "Liên hệ",
  news: "Tin tức",
  risk: "Rủi ro & thông tin bất lợi",
  people: "Người chủ chốt",
  pricing: "Giá bán",
  financials: "Tài chính công khai",
  partners: "Khách hàng, đối tác & dự án",
};
const statusLabels = {
  queued: "Đang chờ",
  running: "Đang nghiên cứu",
  completed: "Hoàn thành",
  report_ready: "Báo cáo sẵn sàng",
  partial: "Có giới hạn dữ liệu",
  needs_input: "Cần xác nhận",
  failed: "Chưa hoàn thành",
  pending: "Chờ phê duyệt",
  approved: "Đã duyệt",
  rejected: "Đã từ chối",
  expired: "Hết hạn",
  executing: "Đang thực hiện",
  succeeded: "Đã thực hiện",
  outcome_unknown: "Chưa xác định kết quả",
  not_started: "Chưa thực hiện",
};
const sourceLabels = {
  official: "Website chính thức",
  website: "Website",
  news: "Tin tức / báo chí",
  social: "Mạng xã hội",
  user: "Người dùng bổ sung",
  sample_email: "Email mẫu từ ảnh bạn cung cấp",
};
const accessLabels = {
  full_text: "Đã đọc nội dung",
  partial_text: "Nội dung có giới hạn",
  fixture: "Nguồn mô phỏng",
  snippet_only: "Chỉ đọc trích đoạn tìm kiếm",
  user_provided: "Người dùng cung cấp",
};
const resultLabels = {
  simulated_send: "Đã mô phỏng gửi thư",
  simulated_trash: "Đã mô phỏng chuyển vào Thùng rác",
  provider_accepted: "Google đã tiếp nhận thư",
  trashed: "Đã chuyển vào Thùng rác",
  already_in_trash: "Thư đã ở trong Thùng rác",
  created: "Đã tạo sự kiện trong Google Calendar",
  already_exists: "Sự kiện này đã có trong Google Calendar",
  simulated_create: "Đã mô phỏng tạo sự kiện",
  deleted: "Đã xóa sự kiện khỏi Google Calendar",
  already_deleted: "Sự kiện đã được xóa trước đó",
  simulated_delete: "Đã mô phỏng xóa sự kiện",
};
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on") && typeof value === "function")
      node.addEventListener(key.slice(2), value);
    else if (key === "value") node.value = value;
    else if (key === "checked") node.checked = value;
    else node.setAttribute(key, String(value));
  }
  for (const child of children.flat(Infinity))
    if (child !== null && child !== undefined)
      node.append(
        child instanceof Node ? child : document.createTextNode(String(child)),
      );
  return node;
}
function safeLink(url, title) {
  try {
    const parsed = new URL(url);
    if (
      !["http:", "https:"].includes(parsed.protocol) ||
      parsed.username ||
      parsed.password
    )
      throw new Error();
    return el(
      "a",
      { href: parsed.href, target: "_blank", rel: "noopener noreferrer" },
      title || parsed.hostname,
    );
  } catch {
    return el("span", {}, title || "Không có đường dẫn công khai");
  }
}
function notice(message, error = false) {
  $("notice").hidden = false;
  $("notice").className = "notice" + (error ? " error" : "");
  $("notice").textContent = message;
}
async function api(path, options = {}) {
  const headers = {
    "X-Research-Request": "1",
    ...(options.body ? { "Content-Type": "application/json" } : {}),
    ...options.headers,
  };
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) {
    location.assign("/login");
    throw new Error("Phiên đã hết hạn.");
  }
  const body = await response.json();
  if (!response.ok)
    throw new Error(
      body.message ||
        (body.detail && typeof body.detail === "string" && body.detail) ||
        "Không thực hiện được yêu cầu. Kiểm tra dữ liệu và quyền truy cập.",
    );
  return body;
}
async function guarded(button, task) {
  button.disabled = true;
  try {
    return await task();
  } catch (error) {
    notice(error.message, true);
  } finally {
    button.disabled = false;
  }
}
function badge(status) {
  const kind = ["report_ready", "completed", "succeeded"].includes(status)
    ? "good"
    : ["failed", "outcome_unknown"].includes(status)
      ? "bad"
      : "warn";
  return el("span", { class: "badge " + kind }, statusLabels[status] || status);
}
function formatDate(value, tz = state.config?.timezone || "Asia/Ho_Chi_Minh") {
  if (!value) return "Chưa có ngày";
  try {
    return new Intl.DateTimeFormat("vi-VN", {
      dateStyle: "short",
      timeStyle: "short",
      timeZone: tz,
    }).format(new Date(value));
  } catch {
    return value;
  }
}
function showView(name) {
  for (const view of ["home", "report", "approval"])
    $(view + "View").hidden = view !== name;
  $("app").classList.toggle("report-mode", name !== "home");
}
async function loadMail() {
  const data = await api(
    "/api/research/mail?query=" + encodeURIComponent($("mailQuery").value),
  );
  state.messages = data.messages;
  state.selected.clear();
  $("selectAll").checked = false;
  $("mailCount").textContent = data.messages.length + " thư";
  $("mailList").replaceChildren(
    ...data.messages.map((message) => {
      const checkbox = el("input", {
        type: "checkbox",
        "aria-label": "Chọn " + message.subject,
        onchange: (event) => {
          if (event.target.checked) state.selected.add(message.id);
          else state.selected.delete(message.id);
        },
      });
      checkbox.dataset.messageId = message.id;
      return el(
        "label",
        { class: "mail" },
        checkbox,
        el(
          "div",
          {},
          el("strong", {}, message.from),
          el("div", { class: "mail-subject" }, message.subject),
          el("small", {}, message.snippet || message.date),
        ),
      );
    }),
  );
  if (!data.messages.length)
    $("mailList").append(
      el("p", { class: "muted" }, "Không tìm thấy email trong phạm vi này."),
    );
  $("scopeNote").textContent = data.next_page_token
    ? "Đang hiển thị tối đa 20 email. Thu hẹp truy vấn để chọn đúng phạm vi."
    : "Chỉ xử lý email bạn chọn. Không tự động gửi hoặc chuyển thư vào Thùng rác.";
  if (state.config.demo_dataset === "customer")
    $("scopeNote").textContent =
      "Ảnh chưa có địa chỉ email gửi, tiêu đề hoặc ngày gửi. Địa chỉ @example.invalid và tiêu đề hiển thị chỉ là chỗ giữ chỗ cho demo.";
  if (data.errors?.length)
    notice(data.errors.map((item) => item.message).join(" "), true);
}
async function loadBatches() {
  const data = await api("/api/research-batches");
  state.batches = data.batches;
  await refreshRuns();
}
async function refreshRuns() {
  const full = await Promise.all(
    state.batches
      .slice(0, 30)
      .map((batch) =>
        api("/api/research-batches/" + encodeURIComponent(batch.id)),
      ),
  );
  state.batches = full;
  renderCompanies();
}
function renderCompanies() {
  const byCompany = new Map();
  for (const batch of state.batches)
    for (const run of batch.runs || []) {
      if (!byCompany.has(run.company.domain))
        byCompany.set(run.company.domain, run);
    }
  $("companyCount").textContent = byCompany.size + " công ty";
  if (!byCompany.size) return;
  $("companyGrid").replaceChildren(
    ...[...byCompany.values()].map((run) => {
      const open = el(
        "button",
        {
          type: "button",
          onclick: () => {
            if (run.report_id)
              location.assign("/reports/" + encodeURIComponent(run.report_id));
            else notice(run.error?.message || "Báo cáo đang được chuẩn bị.");
          },
        },
        run.report_id ? "Mở hồ sơ doanh nghiệp ↗" : "Xem trạng thái",
      );
      return el(
        "article",
        { class: "company-card" },
        el(
          "div",
          { class: "monogram" },
          run.company.name.charAt(0).toUpperCase(),
        ),
        el("h3", {}, run.company.name),
        el("p", { class: "muted" }, run.company.domain),
        el(
          "div",
          { class: "meta" },
          badge(run.status),
          el(
            "span",
            { class: "small muted" },
            run.message_ids.length + " email",
          ),
        ),
        run.error ? el("p", { class: "small" }, run.error.message) : null,
        open,
      );
    }),
  );
}
async function startResearch(selections = []) {
  if (!state.selected.size)
    throw new Error("Chọn ít nhất một email để nghiên cứu.");
  const payload = {
    message_ids: [...state.selected],
    selections,
    timezone: $("timezone").value,
    duration_minutes: Number($("duration").value),
  };
  const batch = await api("/api/research", {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify(payload),
  });
  state.batch = batch.id;
  showView("home");
  notice(
    "Đã bắt đầu nghiên cứu. Các công ty được xử lý riêng, lịch được kiểm tra song song.",
  );
  await pollBatch(batch.id);
}
async function pollBatch(id) {
  clearTimeout(state.poll);
  try {
    const batch = await api("/api/research-batches/" + encodeURIComponent(id));
    const previous = state.batches.findIndex((item) => item.id === id);
    if (previous >= 0) state.batches[previous] = batch;
    else state.batches.unshift(batch);
    renderCompanies();
    $("batchProgress").replaceChildren(
      el(
        "div",
        { class: "progress" },
        statusLabels[batch.status] || batch.status,
        " · ",
        batch.runs.length + " công ty",
        batch.elapsed_seconds !== undefined
          ? " · " + batch.elapsed_seconds + " giây"
          : "",
      ),
    );
    $("unresolved").replaceChildren(
      ...(batch.unresolved || []).map((item) => {
        const name = el("input", {
          placeholder: "Tên công ty",
          value: item.name || "",
          required: true,
          "aria-label": "Tên công ty",
        });
        const site = el("input", {
          placeholder: "https://website-cong-ty.vn",
          value: item.candidates?.[0] || "",
          required: true,
          "aria-label": "Website công ty",
        });
        const button = el(
          "button",
          { type: "submit", class: "primary" },
          "Xác nhận và nghiên cứu",
        );
        return el(
          "div",
          { class: "unresolved" },
          el("h3", {}, "Cần xác định công ty"),
          el("p", {}, item.subject || item.message_id),
          el("p", { class: "muted" }, item.message),
          el(
            "form",
            {
              onsubmit: (event) => {
                event.preventDefault();
                guarded(button, async () => {
                  state.selected = new Set([item.message_id]);
                  await startResearch([
                    {
                      message_id: item.message_id,
                      name: name.value,
                      website: site.value,
                    },
                  ]);
                });
              },
            },
            name,
            site,
            button,
          ),
        );
      }),
    );
    if (["queued", "running"].includes(batch.status))
      state.poll = setTimeout(() => pollBatch(id), 1800);
    else {
      $("researchButton").disabled = false;
      await loadActions();
      if (batch.error) notice(batch.error.message, true);
      else if (batch.errors?.length)
        notice(batch.errors.map((error) => error.message).join(" "), true);
    }
  } catch (error) {
    $("researchButton").disabled = false;
    notice(error.message, true);
  }
}
function citations(fact, report) {
  return el(
    "span",
    { class: "citations" },
    fact.provenance === "user"
      ? el(
          "span",
          { class: "source-type" },
          "Người dùng bổ sung · chưa xác minh",
        )
      : null,
    fact.provenance === "demo"
      ? el("span", { class: "source-type" }, "DEMO")
      : null,
    fact.category === "risk" && fact.source_ids.length === 1
      ? el(
          "span",
          { class: "source-type" },
          "Nguồn đơn lẻ · chưa đối chiếu độc lập",
        )
      : null,
    fact.status === "allegation"
      ? el(
          "span",
          { class: "source-type" },
          "Phản ánh/cáo buộc · chưa có kết luận",
        )
      : null,
    fact.status === "investigation"
      ? el("span", { class: "source-type" }, "Đang điều tra theo nguồn")
      : null,
    ...fact.source_ids.map((id) =>
      el(
        "a",
        { href: "#" + anchorId(report, id) },
        "[" +
          (report.sources.findIndex((source) => source.id === id) + 1) +
          "] ",
      ),
    ),
  );
}
function factNode(fact, report) {
  return el("div", { class: "fact" }, fact.text, citations(fact, report));
}
function factsFor(report, categories) {
  const facts = report.facts.filter((fact) =>
    categories.includes(fact.category),
  );
  return facts.length
    ? facts.map((fact) => factNode(fact, report))
    : [
        el(
          "p",
          { class: "muted small" },
          "Chưa tìm thấy thông tin có nguồn trong phạm vi đã kiểm tra.",
        ),
      ];
}
function section(number, title, ...children) {
  // Collapsible; only the overview starts open so the page stays compact.
  return el(
    "details",
    { class: "report-section", ...(number === "0" ? { open: "" } : {}) },
    el(
      "summary",
      {},
      el("h2", {}, el("span", { class: "section-no" }, number), title),
      el("span", { class: "chev", "aria-hidden": "true" }),
    ),
    el("div", { class: "section-body" }, ...children),
  );
}
function toggleSections(open) {
  for (const item of document.querySelectorAll("#reportView details.report-section"))
    item.open = open;
}
function expandControls() {
  return el(
    "span",
    { class: "expand-controls" },
    el("button", { class: "quiet", type: "button", onclick: () => toggleSections(true) }, "Mở tất cả"),
    el("button", { class: "quiet", type: "button", onclick: () => toggleSections(false) }, "Thu gọn tất cả"),
  );
}
function draftForm(
  report,
  messageId,
  draft = { to: [], subject: "", body: "" },
  action = null,
) {
  const to = el("input", {
    value: draft.to.join(", "),
    required: true,
    "aria-label": "Người nhận",
  });
  const subject = el("input", {
    value: draft.subject,
    required: true,
    maxlength: 500,
    "aria-label": "Tiêu đề",
  });
  const body = el(
    "textarea",
    { required: true, maxlength: 20000, "aria-label": "Nội dung email" },
    draft.body,
  );
  const button = el(
    "button",
    { type: "submit", class: "primary" },
    action ? "Lưu phiên bản mới" : "Đưa bản nháp vào hàng chờ duyệt",
  );
  return el(
    "form",
    {
      class: action ? "action-edit" : "draft-form",
      onsubmit: (event) => {
        event.preventDefault();
        guarded(button, async () => {
          const send = {
            to: to.value
              .split(",")
              .map((value) => value.trim())
              .filter(Boolean),
            subject: subject.value,
            body: body.value,
          };
          if (action)
            await api("/api/actions/" + action.id, {
              method: "PATCH",
              body: JSON.stringify({ version: action.version, send }),
            });
          else
            await api("/api/actions", {
              method: "POST",
              body: JSON.stringify({
                run_id: report.run_id,
                message_id: messageId,
                kind: "send_email",
                send,
              }),
            });
          notice(
            action
              ? "Đã lưu nội dung mới. Cần duyệt đúng phiên bản này."
              : "Bản nháp đã vào hàng chờ. Chưa có email nào được gửi.",
          );
          await showApprovals();
        });
      },
    },
    el("label", {}, "Người nhận (phân cách bằng dấu phẩy)", to),
    el("label", {}, "Tiêu đề", subject),
    el("label", {}, "Nội dung đầy đủ", body),
    button,
  );
}
function renderCalendar(report) {
  const value = report.calendar;
  const availability = el(
    "div",
    { class: "card" },
    el("h3", {}, value.demo ? "Lịch tuần sau (mô phỏng)" : "Lịch tuần sau"),
    el(
      "p",
      { class: "small muted" },
      formatDate(value.start, value.timezone) +
        " → " +
        formatDate(value.end, value.timezone),
    ),
    el(
      "p",
      { class: "small muted" },
      value.timezone + " · Kiểm tra " + formatDate(value.checked_at),
    ),
  );
  if (value.status !== "checked")
    availability.append(
      el(
        "p",
        { class: "limitation" },
        value.error?.message || "Không kiểm tra được lịch.",
      ),
    );
  else {
    availability.append(
      el(
        "p",
        { class: "small" },
        value.busy_count + " khoảng bận · Các khoảng trống trong giờ làm việc:",
      ),
    );
    if (!value.free_windows.length)
      availability.append(
        el(
          "p",
          { class: "small muted" },
          "Không có khoảng trống trong giờ làm việc đã cấu hình.",
        ),
      );
    for (const slot of value.free_windows)
      availability.append(
        el(
          "div",
          { class: "calendar-window" },
          el("span", {}, formatDate(slot.start, value.timezone)),
          " → " + formatDate(slot.end, value.timezone),
        ),
      );
  }
  const assessment = el(
    "div",
    { class: "card" },
    el("h3", {}, "Đề nghị hẹn trong email"),
  );
  for (const meeting of report.meetings) {
    const message = report.messages.find(
      (item) => item.id === meeting.message_id,
    );
    assessment.append(
      el("h4", {}, message?.subject || meeting.message_id),
      el("p", { class: "small" }, meeting.message || meeting.status),
    );
    if (meeting.proposal)
      assessment.append(
        el(
          "p",
          { class: "small muted" },
          formatDate(meeting.proposal.start, value.timezone) +
            " → " +
            formatDate(meeting.proposal.end, value.timezone),
        ),
      );
    if (meeting.draft)
      assessment.append(
        el(
          "details",
          {},
          el("summary", {}, "Xem và chỉnh sửa email đề xuất giờ khác"),
          draftForm(report, meeting.message_id, meeting.draft),
        ),
      );
  }
  assessment.append(
    el(
      "p",
      { class: "small muted" },
      "Các giờ trống chưa được giữ chỗ. Hệ thống kiểm tra lại trước khi gửi email đề xuất.",
    ),
  );
  return el("div", { class: "cards two" }, availability, assessment);
}
function renderKnowledge(report) {
  const category = el(
    "select",
    { "aria-label": "Loại tri thức" },
    ...Object.entries(labels)
      .filter(([key]) =>
        [
          "overview",
          "industry",
          "products",
          "scale",
          "contacts",
          "news",
          "risk",
          "people",
          "pricing",
        ].includes(key),
      )
      .map(([key, label]) => el("option", { value: key }, label)),
  );
  const text = el("textarea", {
    required: true,
    maxlength: 2000,
    "aria-label": "Nội dung bổ sung",
  });
  const source = el("input", {
    type: "url",
    placeholder: "https://… (không bắt buộc)",
    "aria-label": "Nguồn tri thức",
  });
  const save = el(
    "button",
    { type: "submit", class: "primary" },
    "Lưu tri thức",
  );
  const history = el("div", {});
  const historyButton = el(
    "button",
    {
      type: "button",
      class: "quiet",
      onclick: () =>
        guarded(historyButton, async () => {
          const data = await api(
            "/api/companies/" + report.company.id + "/knowledge/history",
          );
          history.replaceChildren(
            ...data.revisions.map((item) =>
              el(
                "div",
                { class: "history-entry" },
                "v" +
                  item.version +
                  " · " +
                  formatDate(item.changed_at) +
                  " · " +
                  item.actor,
              ),
            ),
          );
        }),
    },
    "Xem lịch sử phiên bản",
  );
  let currentVersion = report.company.version;
  const form = el(
    "form",
    {
      class: "knowledge-form",
      onsubmit: (event) => {
        event.preventDefault();
        guarded(save, async () => {
          const updated = await api(
            "/api/companies/" + report.company.id + "/knowledge",
            {
              method: "PATCH",
              body: JSON.stringify({
                expected_version: currentVersion,
                category: category.value,
                text: text.value,
                source_url: source.value,
              }),
            },
          );
          currentVersion = updated.version;
          notice(
            "Đã lưu tri thức v" +
              currentVersion +
              ". Báo cáo hiện tại giữ nguyên phiên bản " +
              report.company.version +
              "; nghiên cứu lại để dùng dữ liệu mới.",
          );
          text.value = "";
        });
      },
    },
    el("label", {}, "Nhóm thông tin", category),
    el("label", {}, "Bổ sung hoặc đính chính", text),
    el("label", {}, "Nguồn tham khảo", source),
    save,
  );
  api("/api/companies/" + report.company.id)
    .then((company) => {
      currentVersion = company.version;
    })
    .catch(() => {});
  return el(
    "div",
    { class: "card" },
    el(
      "p",
      { class: "small muted" },
      "Thông tin bạn bổ sung được ghi nguồn và lịch sử. Nội dung chưa kiểm chứng được đánh dấu rõ; báo cáo đã tạo không bị sửa ngầm.",
    ),
    form,
    el("p", {}, historyButton),
    history,
  );
}
function renderPeople(report) {
  if (!report.people.length)
    return el(
      "div",
      { class: "card" },
      ...factsFor(report, ["people"]),
      el(
        "p",
        { class: "small muted" },
        "Chưa có đủ bằng chứng để dựng hồ sơ riêng cho một người.",
      ),
    );
  return el(
    "div",
    {},
    ...report.people.map((person) =>
      el(
        "div",
        {},
        el("h3", {}, person.name),
        el(
          "div",
          { class: "cards" },
          ...["career", "relationships", "profiles"].map((key, index) =>
            el(
              "div",
              { class: "card" },
              el(
                "h3",
                {},
                [
                  "Vai trò & thành tích",
                  "Quan hệ nghề nghiệp có nguồn",
                  "Hồ sơ công khai",
                ][index],
              ),
              person[key].length
                ? person[key].map((id) =>
                    factNode(
                      report.facts.find((fact) => fact.id === id),
                      report,
                    ),
                  )
                : el(
                    "p",
                    { class: "small muted" },
                    "Chưa tìm thấy thông tin có nguồn.",
                  ),
            ),
          ),
        ),
      ),
    ),
  );
}
function renderResearchScope(report) {
  const scope = report.research_scope;
  if (!scope || !Array.isArray(scope.topics)) return null;
  const statuses = {
    read: "Đã đọc nguồn",
    no_results: "Chưa có kết quả",
    no_readable_sources: "Chưa đọc được nguồn",
    search_failed: "Tìm kiếm có lỗi",
    not_searched: "Chưa tìm kiếm",
  };
  return el(
    "div",
    { class: "card research-scope" },
    el("h3", {}, "Phạm vi nghiên cứu từ nhiều nguồn"),
    el(
      "p",
      { class: "muted" },
      (scope.sources || []).length +
        " trang đã đọc · " +
        scope.unique_domains +
        " tên miền · " +
        scope.topics.filter((topic) => topic.searched).length +
        "/" +
        scope.topics.length +
        " nhóm đã tìm",
    ),
    el(
      "p",
      { class: "small muted" },
      "Lượt nghiên cứu: " +
        formatDate(scope.searched_at) +
        (report.memory_reused ? " · Dùng tri thức đã lưu" : ""),
    ),
    el(
      "div",
      { class: "cards two" },
      ...scope.topics.map((topic) =>
        el(
          "div",
          { class: "source" },
          el("strong", {}, topic.label),
          el(
            "p",
            { class: "small muted" },
            (statuses[topic.status] || topic.status) +
              " · " +
              topic.read_sources.length +
              " nguồn đã đọc",
          ),
        ),
      ),
    ),
    el(
      "details",
      {},
      el("summary", {}, "Xem tất cả trang đã đọc"),
      ...(scope.sources || []).map((source) =>
        el(
          "p",
          { class: "small" },
          source.url
            ? safeLink(source.url, source.title || source.url)
            : source.title,
        ),
      ),
    ),
    el("p", { class: "small muted" }, scope.limitation),
    el(
      "p",
      { class: "small muted" },
      "Một trang có thể được tìm thấy ở nhiều nhóm. Đã đọc nguồn không đồng nghĩa đã xác minh đủ mọi trường thông tin.",
    ),
  );
}

const riskLabels = {
  source_statement: "Nguồn đề cập",
  allegation: "Phản ánh/cáo buộc · chưa có kết luận",
  investigation: "Đang điều tra theo nguồn",
  concluded_finding: "Đã có kết luận theo nguồn",
};
const priceLabels = {
  public: ["CÓ GIÁ CÔNG KHAI", "good"],
  not_public: ["GIÁ KHÔNG CÔNG KHAI", "warn"],
  free: ["MIỄN PHÍ", "good"],
};
function anchorId(report, id) {
  return report.id.slice(0, 8) + "-" + id;
}
function cite(ids, report) {
  return el(
    "span",
    { class: "citations inline" },
    ...(ids || []).map((id) => {
      const index = report.sources.findIndex((source) => source.id === id);
      return index < 0
        ? null
        : el("a", { href: "#" + anchorId(report, id) }, "[" + (index + 1) + "] ");
    }),
  );
}
function flags(item) {
  return [
    item.estimated ? el("span", { class: "source-type" }, "Ước tính") : null,
    item.verified === false
      ? el("span", { class: "source-type" }, "Chưa đối chiếu nguyên văn")
      : null,
  ];
}
function citedText(item, report) {
  return el("span", {}, item.text, " ", flags(item), cite(item.source_ids, report));
}
function dxList(items, report, render = (item) => citedText(item, report)) {
  return items.length
    ? el("ul", { class: "dx-list" }, ...items.map((item) => el("li", {}, render(item))))
    : el("p", { class: "muted small" }, "Chưa tìm thấy thông tin có nguồn.");
}
function dxCard(title, dot, ...children) {
  return el(
    "div",
    { class: "dx-card" },
    el("h3", {}, el("span", { class: "dx-dot " + (dot || "") }), title),
    ...children,
  );
}
function dxCallout(label, content) {
  return el("div", { class: "dx-callout" }, el("strong", {}, label), " ", content);
}
function dxRows(rows, report) {
  return rows.length
    ? el(
        "div",
        { class: "dx-rows" },
        ...rows.map((row) =>
          el(
            "div",
            { class: "dx-row" },
            el("span", {}, row.label),
            el("div", {}, row.value, " ", cite(row.source_ids, report)),
          ),
        ),
      )
    : el("p", { class: "muted small" }, "Chưa tìm thấy thông tin có nguồn.");
}
function dxHeader(report, dossier) {
  const head = dossier.headline;
  const pills = [
    head.status ? ["dot", head.status] : null,
    head.hq ? ["", "Trụ sở: " + head.hq] : null,
    head.industry ? ["", "Lĩnh vực: " + head.industry] : null,
    head.size_label ? ["warn", "Quy mô: " + head.size_label] : null,
  ].filter(Boolean);
  const demo =
    report.mode === "demo"
      ? report.demo_dataset === "customer"
        ? "EMAIL MẪU · CHƯA XÁC MINH WEBSITE"
        : "DEMO · DỮ LIỆU MÔ PHỎNG"
      : null;
  return el(
    "div",
    { class: "report-header" },
    el(
      "div",
      { class: "eyebrow" },
      "NGHIÊN CỨU THỊ TRƯỜNG / " + (head.name || report.company.name).toUpperCase() +
        (head.tagline ? " — " + head.tagline : ""),
    ),
    el("h1", {}, (head.name || report.company.name) + " — Hồ sơ Doanh nghiệp & Dashboard"),
    el(
      "div",
      { class: "tags" },
      demo ? el("span", { class: "badge demo" }, demo) : null,
      ...pills.map(([kind, text]) =>
        el(
          "span",
          { class: "badge " + (kind === "warn" ? "warn" : kind === "dot" ? "good" : "") },
          text,
        ),
      ),
    ),
    el(
      "p",
      { class: "muted small" },
      "Tạo " + formatDate(report.generated_at) + " · " + report.messages.length +
        " email · " +
        (report.memory_reused ? "dùng tri thức đã lưu" : "nghiên cứu mới") +
        (dossier.mode === "agent" ? " · Search agent" : " · tổng hợp từ nguồn đã đọc") +
        (head.website ? " · " : ""),
      head.website ? safeLink(head.website, head.website.replace(/^https?:\/\//, "")) : null,
    ),
    head.identity_note
      ? dxCallout("Xác định công ty:", head.identity_note)
      : null,
  );
}
function dxOverview(report, dossier) {
  const month = new Date(report.generated_at);
  return section(
    "0",
    "Tổng quan nhanh",
    el(
      "p",
      { class: "muted small dx-sub" },
      "Các chỉ số chính tổng hợp từ nguồn công khai (" +
        String(month.getMonth() + 1).padStart(2, "0") + "/" + month.getFullYear() + ")",
    ),
    dossier.kpis.length
      ? el(
          "div",
          { class: "dx-kpis" },
          ...dossier.kpis.map((kpi, index) =>
            el(
              "div",
              { class: "dx-kpi c" + (index % 5) },
              el(
                "strong",
                {},
                kpi.value,
                kpi.unit ? el("small", {}, " " + kpi.unit) : null,
              ),
              el("span", {}, kpi.label, " ", kpi.estimated ? "(ước tính) " : "", cite(kpi.source_ids, report)),
              el("i", {}),
            ),
          ),
        )
      : el("p", { class: "muted small" }, "Chưa tìm thấy chỉ số định lượng có nguồn."),
    dossier.summary
      ? dxCallout("Tóm tắt một dòng:", citedText(dossier.summary, report))
      : null,
  );
}
function dxGeneral(report, dossier) {
  const team = dossier.team;
  const size = [team.size, ...team.signals].filter(Boolean);
  return section(
    "1",
    "Thông tin chung & Đội ngũ",
    el(
      "div",
      { class: "dx-grid three" },
      dxCard("Hồ sơ công ty", "", dxRows(dossier.profile, report)),
      dxCard(
        "Quy mô đội ngũ (ước tính)",
        "mint",
        dxList(size, report),
        team.culture
          ? el("h4", {}, "Văn hóa & tuyển dụng")
          : null,
        team.culture ? el("p", { class: "small" }, citedText(team.culture, report)) : null,
      ),
      dxCard(
        "Vị trí đang tuyển",
        "pink",
        team.openings.length
          ? el(
              "div",
              { class: "dx-rows narrow" },
              ...team.openings.map((job) =>
                el(
                  "div",
                  { class: "dx-row" },
                  el("span", { class: "plain" }, job.title),
                  el("div", { class: "muted" }, job.kind, " ", cite(job.source_ids, report)),
                ),
              ),
            )
          : el("p", { class: "muted small" }, "Chưa tìm thấy vị trí đang tuyển có nguồn."),
        team.hiring_note ? dxCallout("Lưu ý:", citedText(team.hiring_note, report)) : null,
      ),
    ),
  );
}
function dxFounders(report, dossier) {
  if (!dossier.founders.length)
    return section(
      "2",
      "Nhà sáng lập & người chủ chốt",
      el("p", { class: "muted" }, "Chưa tìm thấy hồ sơ nhà sáng lập có nguồn trong phạm vi đã kiểm tra."),
    );
  return section(
    "2",
    "Nhà sáng lập & người chủ chốt",
    ...dossier.founders.map((person) =>
      el(
        "div",
        { class: "dx-person" },
        el(
          "h3",
          { class: "dx-person-name" },
          person.name,
          person.role || person.subtitle
            ? el("small", {}, " · " + [person.role, person.subtitle].filter(Boolean).join(" · "))
            : null,
        ),
        el(
          "div",
          { class: "dx-grid three" },
          dxCard("Thành tích cốt lõi", "", dxList(person.achievements, report)),
          dxCard(
            "Mạng lưới quan hệ then chốt",
            "mint",
            dxList(person.network, report, (item) =>
              el(
                "span",
                {},
                item.name ? el("b", {}, item.name + " — ") : null,
                item.detail,
                " ",
                cite(item.source_ids, report),
              ),
            ),
          ),
          dxCard(
            "Hồ sơ số & phong cách",
            "yellow",
            person.profiles.length
              ? el(
                  "div",
                  { class: "dx-rows narrow" },
                  ...person.profiles.map((profile) =>
                    el(
                      "div",
                      { class: "dx-row" },
                      el("span", {}, profile.platform),
                      el(
                        "div",
                        {},
                        profile.link
                          ? safeLink(profile.link, profile.handle || profile.link)
                          : el("code", {}, profile.handle),
                        profile.detail ? " · " + profile.detail : "",
                        " ",
                        cite(profile.source_ids, report),
                      ),
                    ),
                  ),
                )
              : el("p", { class: "muted small" }, "Chưa tìm thấy hồ sơ số có nguồn."),
            person.style
              ? el("p", { class: "small dx-style" }, el("b", {}, "Phong cách: "), citedText(person.style, report))
              : null,
          ),
        ),
        person.strength
          ? dxCallout("Điểm mạnh lớn nhất:", citedText(person.strength, report))
          : null,
      ),
    ),
  );
}
function dxProducts(report, dossier) {
  return section(
    "3",
    "Sản phẩm & Giá bán",
    dossier.products.length
      ? el(
          "div",
          { class: "dx-grid two" },
          ...dossier.products.map((product) => {
            const price = priceLabels[product.price_visibility];
            return el(
              "div",
              { class: "dx-card product" },
              el(
                "div",
                { class: "dx-tags" },
                product.tag ? el("span", { class: "dx-tag" }, product.tag.toUpperCase()) : null,
                price ? el("span", { class: "dx-tag " + price[1] }, price[0]) : null,
              ),
              el("h3", {}, product.name, " ", cite(product.source_ids, report)),
              product.summary ? el("p", { class: "small muted" }, product.summary) : null,
              product.bullets.length
                ? el("ul", { class: "dx-list" }, ...product.bullets.map((b) => el("li", {}, b)))
                : null,
              product.prices.length
                ? el(
                    "div",
                    { class: "dx-prices" },
                    ...product.prices.map((p) =>
                      el(
                        "div",
                        { class: "dx-price" },
                        el("span", {}, p.label),
                        el("strong", {}, p.value),
                        p.note ? el("small", {}, p.note) : null,
                      ),
                    ),
                  )
                : null,
              product.callout ? dxCallout("", product.callout) : null,
            );
          }),
        )
      : el("p", { class: "muted" }, "Chưa tìm thấy sản phẩm hoặc giá bán có nguồn."),
  );
}
function dxMarket(report, dossier) {
  return section(
    "4",
    "Khách hàng, đối tác & Tài chính",
    el(
      "div",
      { class: "dx-grid two" },
      dxCard("Khách hàng, đối tác & dự án", "mint", dxList(dossier.customers_partners, report)),
      dxCard("Quy mô & tài chính công khai", "yellow", dxList(dossier.financials, report)),
    ),
  );
}
function dxNews(report, dossier) {
  return section(
    "5",
    "Tin tức 30 ngày & Rủi ro",
    el(
      "div",
      { class: "dx-grid two" },
      dxCard(
        "Tin tức & phát triển",
        "",
        dxList(dossier.news, report, (item) =>
          el(
            "span",
            {},
            item.date ? el("b", {}, item.date + " — ") : null,
            item.text,
            " ",
            cite(item.source_ids, report),
          ),
        ),
      ),
      dxCard(
        "Rủi ro & thông tin bất lợi",
        "red",
        dxList(dossier.risks, report, (item) =>
          el(
            "span",
            {},
            item.text,
            " ",
            el("span", { class: "source-type" }, riskLabels[item.status] || item.status),
            cite(item.source_ids, report),
          ),
        ),
        el(
          "p",
          { class: "small muted" },
          dossier.risk_note || "",
          " ",
          report.risk_scope,
        ),
      ),
    ),
  );
}
function dxScorecard(report, dossier) {
  const rows = report.scorecard || [];
  return el(
    "div",
    { class: "dx-card" },
    el("h3", {}, el("span", { class: "dx-dot" }), "Đánh giá chất lượng báo cáo"),
    el(
      "table",
      { class: "dx-table" },
      el("thead", {}, el("tr", {}, el("th", {}, "Tiêu chí"), el("th", {}, "Kết quả"))),
      el(
        "tbody",
        {},
        ...rows.map((row) =>
          el("tr", {}, el("td", {}, row.criterion), el("td", {}, row.result)),
        ),
      ),
    ),
    dossier.conflicts.length
      ? el("h4", {}, "Số liệu mâu thuẫn giữa các nguồn")
      : null,
    dossier.conflicts.length
      ? el("ul", { class: "dx-list" }, ...dossier.conflicts.map((c) => el("li", {}, c)))
      : null,
    dossier.gaps.length ? el("h4", {}, "Chưa tìm thấy / cần xác minh") : null,
    dossier.gaps.length
      ? el("ul", { class: "dx-list" }, ...dossier.gaps.map((g) => el("li", {}, g)))
      : null,
  );
}

function reportNodes(report) {
  const dossier = report.dossier;
  const q = report.quality;
  const sourceSection = el(
    "div",
    { class: "card" },
    ...report.sources.map((source, index) =>
      el(
        "div",
        { class: "source", id: anchorId(report, source.id) },
        el("strong", {}, "[" + (index + 1) + "] "),
        safeLink(source.url, source.title),
        el(
          "small",
          {},
          (source.demo ? "DEMO · " : "") +
            (sourceLabels[source.kind] || source.kind) +
            " · " +
            (accessLabels[source.access] || source.access) +
            " · Thu thập " +
            formatDate(source.retrieved_at),
        ),
        el(
          "small",
          {},
          "Ngày đăng: " +
            (source.published_at
              ? formatDate(source.published_at)
              : "Chưa xác định"),
        ),
        el(
          "details",
          {},
          el("summary", {}, "Xem trích đoạn nguồn"),
          el("blockquote", {}, source.text.slice(0, 2000)),
        ),
      ),
    ),
  );
  const limitations = el(
    "div",
    { class: "card" },
    el("p", { class: "small" }, q.accuracy),
    el(
      "p",
      { class: "small muted" },
      "Trường còn thiếu: " +
        (q.missing.map((key) => labels[key]).join(", ") ||
          "Không có trong checklist hiện tại."),
    ),
    el("p", { class: "small muted" }, q.readiness),
    ...report.errors.map((error) =>
      el("div", { class: "limitation" }, error.message),
    ),
    el(
      "details",
      {},
      el("summary", {}, "Vai trò trong lượt nghiên cứu"),
      ...Object.entries(report.roles).map(([role, status]) =>
        el("div", { class: "small muted" }, labels[role] + ": " + status),
      ),
    ),
  );
  const mailActions = el(
    "div",
    { class: "card" },
    el("h3", {}, "Email liên quan"),
  );
  for (const message of report.messages) {
    const trash = el(
      "button",
      {
        type: "button",
        class: "quiet",
        onclick: () =>
          guarded(trash, async () => {
            await api("/api/actions", {
              method: "POST",
              body: JSON.stringify({
                run_id: report.run_id,
                message_id: message.id,
                kind: "trash_email",
              }),
            });
            notice(
              "Đã tạo yêu cầu chuyển thư vào Thùng rác. Cần bạn duyệt trước khi thực hiện.",
            );
            await showApprovals();
          }),
      },
      "Đề xuất chuyển vào Thùng rác",
    );
    mailActions.append(
      el(
        "div",
        { class: "mail-reference" },
        el(
          "span",
          {},
          message.subject,
          el("small", { class: "muted" }, " · " + message.from),
        ),
        trash,
      ),
      el(
        "details",
        {},
        el("summary", {}, "Soạn phản hồi"),
        draftForm(report, message.id, {
          to: [(message.from.match(/<([^>]+)>/) || [])[1] || message.from],
          subject: "Re: " + message.subject.replace(/^Re:\s*/i, ""),
          body: "",
        }),
      ),
    );
  }
  const prep = el(
    "div",
    { class: "dx-card" },
    el("h3", {}, el("span", { class: "dx-dot mint" }), "Gợi ý chuẩn bị cuộc hẹn"),
    dossier.meeting_prep.length
      ? el("ul", { class: "dx-list" }, ...dossier.meeting_prep.map((x) => el("li", {}, x)))
      : el("p", { class: "muted small" }, "Chưa có gợi ý; xem dữ liệu thiếu ở mục cuối."),
    el("p", { class: "small muted" }, "Gợi ý do search agent đề xuất từ các phát hiện ở trên; không phải thông tin có nguồn."),
  );
  return [
    dxHeader(report, dossier),
    report.demo_dataset === "customer"
      ? el(
          "div",
          { class: "summary" },
          "Trang này chỉ trình bày dữ liệu từ email mẫu bạn cung cấp. Hồ sơ doanh nghiệp, tin tức và rủi ro chưa được nghiên cứu hoặc xác minh.",
        )
      : null,
    dxOverview(report, dossier),
    dxGeneral(report, dossier),
    dxFounders(report, dossier),
    dxProducts(report, dossier),
    dxMarket(report, dossier),
    dxNews(report, dossier),
    section(
      "6",
      "Chuẩn bị cuộc hẹn",
      report.calendar.status === "not_requested"
        ? el(
            "div",
            { class: "dx-card" },
            el("h3", {}, el("span", { class: "dx-dot" }), "Lịch họp"),
            el("p", { class: "small muted" }, report.calendar.message),
          )
        : renderCalendar(report),
      prep,
    ),
    section("7", "Tri thức công ty", renderKnowledge(report)),
    section("8", "Hành động email", mailActions),
    section(
      "9",
      "Nguồn & chất lượng",
      dxScorecard(report, dossier),
      renderResearchScope(report),
      limitations,
      sourceSection,
    ),
  ];
}
async function openReport(id) {
  const report = await api("/api/reports/" + encodeURIComponent(id));
  state.report = report;
  showView("report");
  $("reportView").replaceChildren(
    el(
      "div",
      { class: "report-top" },
      el("a", { href: "/research" }, "← Tất cả công ty"),
      el(
        "div",
        {},
        expandControls(),
        " · ",
        el("a", { href: "/api/reports/" + report.id + "/export.md" }, "Tải Markdown ↓"),
        " · ",
        el("button", { class: "quiet", type: "button", onclick: () => window.print() }, "In / lưu PDF"),
      ),
    ),
    ...reportNodes(report),
  );
  document.title = report.company.name + " · Briefing Room";
}
async function openBriefing(batchId) {
  const batch = await api("/api/research-batches/" + encodeURIComponent(batchId));
  const reports = [];
  for (const run of batch.runs)
    if (run.report_id)
      reports.push(await api("/api/reports/" + encodeURIComponent(run.report_id)));
  state.report = reports[0] || null;
  showView("report");
  const failed = batch.runs.filter((run) => !run.report_id);
  $("reportView").replaceChildren(
    el(
      "div",
      { class: "report-top" },
      el("a", { href: "/research" }, "← Tất cả công ty"),
      el(
        "div",
        {},
        expandControls(),
        " · ",
        el("button", { class: "quiet", type: "button", onclick: () => window.print() }, "In / lưu PDF"),
      ),
    ),
    el(
      "div",
      { class: "report-header" },
      el("div", { class: "eyebrow" }, "BÁO CÁO NGHIÊN CỨU EMAIL"),
      el("h1", {}, "Hồ sơ " + reports.length + " công ty từ email"),
      el(
        "p",
        { class: "muted small" },
        (statusLabels[batch.status] || batch.status) +
          (batch.elapsed_seconds !== undefined ? " · " + batch.elapsed_seconds + " giây" : "") +
          " · mọi nhận định gắn nguồn đã truy xuất; thông tin thiếu được ghi rõ.",
      ),
      el(
        "nav",
        { class: "dx-jump" },
        ...reports.map((report, index) =>
          el("a", { href: "#company-" + index }, report.dossier.headline.name || report.company.name),
        ),
      ),
    ),
    ...failed.map((run) =>
      el(
        "div",
        { class: "limitation" },
        (run.company?.name || "Công ty") + ": " + (run.error?.message || statusLabels[run.status] || run.status),
      ),
    ),
    ...(batch.unresolved || []).map((item) =>
      el("div", { class: "limitation" }, (item.subject || item.message_id) + ": " + item.message),
    ),
    ...reports.map((report, index) =>
      el("article", { class: "company-report", id: "company-" + index }, ...reportNodes(report)),
    ),
  );
  document.title = "Báo cáo nghiên cứu · Briefing Room";
}
async function loadActions() {
  const data = await api("/api/actions");
  state.actions = data.actions;
  $("approvalCount").textContent = data.actions.filter(
    (action) => action.state === "pending",
  ).length;
}
async function showApprovals() {
  await loadActions();
  showView("approval");
  $("actionList").replaceChildren(
    ...state.actions.map((action) => {
      const card = el(
        "article",
        { class: "action-card" },
        el(
          "div",
          { class: "tags" },
          badge(action.state),
          badge(action.execution),
          action.metadata.mode === "demo"
            ? el("span", { class: "badge demo" }, "DEMO")
            : null,
        ),
        el(
          "h3",
          {},
          {
            send_email: "Gửi email",
            create_event: "Tạo sự kiện Google Calendar",
            delete_event: "XÓA sự kiện Google Calendar",
          }[action.kind] || "Chuyển email vào Thùng rác",
        ),
        el(
          "p",
          { class: "small muted" },
          "Phiên bản " +
            action.version +
            " · Hết hạn " +
            formatDate(new Date(action.expires_at * 1000).toISOString()) +
            " · Mã nội dung " +
            action.payload_hash.slice(0, 12),
        ),
      );
      if (action.kind === "send_email")
        card.append(
          el("p", {}, "Đến: " + action.payload.to.join(", ")),
          el("p", {}, "Tiêu đề: " + action.payload.subject),
          el("div", { class: "action-body" }, action.payload.body),
        );
      else if (action.kind === "delete_event")
        card.append(
          el("p", {}, "Sự kiện: " + action.payload.title),
          el(
            "p",
            {},
            "Thời gian: " +
              formatDate(action.payload.start) +
              " → " +
              formatDate(action.payload.end),
          ),
          action.payload.attendee_count
            ? el("p", { class: "limitation" }, "Có " + action.payload.attendee_count + " người tham dự.")
            : null,
          el("p", { class: "small muted" }, "Xóa khỏi lịch của bạn; không gửi thông báo cho ai."),
        );
      else if (action.kind === "create_event")
        card.append(
          el("p", {}, "Tiêu đề: " + action.payload.title),
          el(
            "p",
            {},
            "Thời gian: " +
              formatDate(action.payload.start, action.payload.timezone) +
              " → " +
              formatDate(action.payload.end, action.payload.timezone),
          ),
          action.payload.location
            ? el("p", {}, "Địa điểm: " + action.payload.location)
            : null,
          action.payload.description
            ? el("div", { class: "action-body" }, action.payload.description)
            : null,
          el(
            "p",
            { class: "small muted" },
            "Sự kiện chỉ của bạn; không mời ai và không gửi thông báo.",
          ),
        );
      else
        card.append(
          el("p", {}, "Email: " + action.payload.message_id),
          el(
            "p",
            { class: "small muted" },
            "Chuyển vào Thùng rác; không xóa vĩnh viễn.",
          ),
        );
      card.append(
        el(
          "p",
          { class: "small muted" },
          "Tài khoản: " + action.metadata.mailbox,
        ),
      );
      if (action.state === "pending") {
        const controls = el("div", { class: "action-controls" });
        for (const decision of ["approve", "reject"]) {
          const button = el(
            "button",
            {
              class: decision === "approve" ? "primary" : "reject",
              type: "button",
              onclick: () =>
                guarded(button, async () => {
                  const result = await api(
                    "/api/actions/" + action.id + "/decision",
                    {
                      method: "POST",
                      body: JSON.stringify({
                        decision,
                        version: action.version,
                        payload_hash: action.payload_hash,
                      }),
                    },
                  );
                  notice(
                    result.error?.message ||
                      (decision === "reject"
                        ? "Đã từ chối. Không thực hiện hành động."
                        : result.metadata.mode === "demo"
                          ? "Đã thực hiện mô phỏng; không thay đổi hộp thư thật."
                          : "Đã ghi nhận kết quả từ Google."),
                    !!result.error,
                  );
                  await showApprovals();
                }),
            },
            decision === "approve" ? "Duyệt và thực hiện" : "Từ chối",
          );
          controls.append(button);
        }
        card.append(controls);
        if (action.kind === "send_email")
          card.append(
            el(
              "details",
              {},
              el("summary", {}, "Chỉnh sửa trước khi duyệt"),
              draftForm(null, null, action.payload, action),
            ),
          );
      }
      if (action.result)
        card.append(
          el(
            "p",
            { class: "small" },
            "Kết quả: " +
              (resultLabels[action.result.status] || action.result.status) +
              (action.result.delivery_confirmed === false
                ? " · Chưa xác nhận người nhận đã nhận/đọc."
                : ""),
          ),
        );
      if (action.result?.html_link)
        card.append(
          el("p", { class: "small" }, safeLink(action.result.html_link, "Mở sự kiện trong Google Calendar")),
        );
      if (action.error)
        card.append(el("p", { class: "limitation" }, action.error.message));
      if (action.execution === "outcome_unknown")
        card.append(
          el(
            "p",
            { class: "limitation" },
            "Không tự động gửi lại. Kiểm tra thư đã gửi trước khi tạo hành động mới.",
          ),
        );
      return card;
    }),
  );
  if (!state.actions.length)
    $("actionList").append(
      el(
        "div",
        { class: "empty" },
        el("h3", {}, "Chưa có yêu cầu cần duyệt"),
        el(
          "p",
          {},
          "Tạo bản nháp, đề xuất chuyển thư vào Thùng rác từ một báo cáo, hoặc nhờ trợ lý tạo lịch trong Chat.",
        ),
      ),
    );
}
async function init() {
  try {
    state.config = await api("/api/research/config");
    if (state.config.mode === "demo")
      document.querySelector('nav a[href="/chat"]').hidden = true;
    $("accountEmail").textContent = state.config.email;
    $("modeBadge").textContent =
      state.config.mode === "demo" ? "DEMO" : "GOOGLE";
    $("modeBadge").className =
      "badge " + (state.config.mode === "demo" ? "demo" : "good");
    $("demoBanner").hidden = state.config.mode !== "demo";
    $("demoDatasetForm").hidden = state.config.mode !== "demo";
    if (state.config.mode === "demo") {
      $("demoDataset").value = state.config.demo_dataset;
      $("demoDatasetForm").addEventListener("submit", (event) => {
        event.currentTarget.action =
          "/auth/demo?dataset=" + encodeURIComponent($("demoDataset").value);
      });
      if (state.config.demo_dataset === "customer")
        $("demoBanner").textContent =
          "EMAIL MẪU · Tên công ty, website và nội dung theo ảnh bạn cung cấp. Chưa nghiên cứu website thật; lịch và thao tác email ở chế độ mô phỏng.";
    }

    $("connectGoogle").hidden = state.config.mode === "demo";
    $("roleStrip").replaceChildren(
      ...state.config.roles.map((role, index) =>
        el(
          "div",
          { class: "role" },
          el("span", {}, String(index + 1).padStart(2, "0")),
          labels[role],
        ),
      ),
    );
    $("mailSearch").addEventListener("submit", (event) => {
      event.preventDefault();
      guarded(event.submitter, loadMail);
    });
    $("selectAll").addEventListener("change", (event) => {
      state.selected = new Set(
        event.target.checked ? state.messages.map((message) => message.id) : [],
      );
      for (const input of $("mailList").querySelectorAll("input"))
        input.checked = event.target.checked;
    });
    $("researchForm").addEventListener("submit", (event) => {
      event.preventDefault();
      guarded($("researchButton"), () => startResearch());
    });
    $("approvalNav").addEventListener("click", () =>
      showApprovals().catch((error) => notice(error.message, true)),
    );
    await loadActions();
    const wantsApprovals =
      new URLSearchParams(location.search).get("view") === "approvals";
    const match = location.pathname.match(/^\/reports\/([^/]+)$/);
    const briefing = location.pathname.match(/^\/briefings\/([^/]+)$/);
    if (match) await openReport(decodeURIComponent(match[1]));
    else if (briefing) await openBriefing(decodeURIComponent(briefing[1]));
    else {
      await loadBatches();
      await loadMail();
      const active = state.batches.find((batch) =>
        ["queued", "running"].includes(batch.status),
      );
      if (active) await pollBatch(active.id);
      if (wantsApprovals) await showApprovals();
    }
  } catch (error) {
    notice(error.message, true);
  }
}
document.addEventListener("click", (event) => {
  const link = event.target.closest?.('a[href^="#"]');
  if (!link || link.getAttribute("href").length < 2) return;
  const target = document.getElementById(
    decodeURIComponent(link.getAttribute("href").slice(1)),
  );
  for (let node = target?.parentElement; node; node = node.parentElement)
    if (node.tagName === "DETAILS") node.open = true;
});
window.addEventListener("beforeprint", () => toggleSections(true));
init();
