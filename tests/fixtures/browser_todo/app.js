const demo = document.querySelector("#experienceDemo");
const stateLabel = document.querySelector("#demoState");
const summary = document.querySelector("#demoSummary");
const heroStart = document.querySelector("#heroStart");
const startButton = document.querySelector("#startDemo");
const resultButton = document.querySelector("#viewResult");
const resetButton = document.querySelector("#resetDemo");
const stepResults = [...document.querySelectorAll(".step-result")];
const todoForm = document.querySelector("#todoForm");
const todoInput = document.querySelector("#todoInput");
const addTaskButton = document.querySelector("#addTask");
const todoList = document.querySelector("#todoList");
const todoStatus = document.querySelector("#todoStatus");
const todoCount = document.querySelector("#todoCount");
const filterButtons = [...document.querySelectorAll("[data-filter]")];

let tasks = [];
let activeFilter = "all";
let requestInProgress = false;

const states = {
  idle: {
    label: "准备就绪",
    summary: "准备好后，点击“开始体验”进入流程。",
    steps: ["等待中", "等待中", "等待中"],
    visibleButton: startButton,
  },
  progress: {
    label: "正在推进",
    summary: "演示流程已推进到成果检查节点，可以查看本次模拟交付。",
    steps: ["已识别", "已规划", "待查看"],
    visibleButton: resultButton,
  },
  complete: {
    label: "已完成",
    summary: "READY_TO_REVIEW · 模拟成果已整理完成，可从头体验整个流程。",
    steps: ["已确认", "已执行", "已交付"],
    visibleButton: resetButton,
  },
};

function setState(nextState) {
  const content = states[nextState];

  demo.dataset.state = nextState;
  stateLabel.textContent = content.label;
  summary.textContent = content.summary;
  stepResults.forEach((result, index) => {
    result.textContent = content.steps[index];
  });

  [startButton, resultButton, resetButton].forEach((button) => {
    button.hidden = button !== content.visibleButton;
  });
}

function startExperience({ scroll = false } = {}) {
  setState("progress");
  if (scroll) {
    demo.scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

heroStart.addEventListener("click", () => startExperience({ scroll: true }));
startButton.addEventListener("click", () => startExperience());
resultButton.addEventListener("click", () => setState("complete"));
resetButton.addEventListener("click", () => setState("idle"));

function setTodoStatus(message, tone = "neutral") {
  todoStatus.textContent = message;
  todoStatus.dataset.tone = tone;
}

function setRequestInProgress(isBusy) {
  requestInProgress = isBusy;
  todoInput.disabled = isBusy;
  addTaskButton.disabled = isBusy;
  todoList.querySelectorAll("input[type='checkbox']").forEach((checkbox) => {
    checkbox.disabled = isBusy;
  });
}

async function requestJSON(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      Accept: "application/json",
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || `请求失败（${response.status}）`);
  }
  return payload;
}

function taskCounts() {
  const completed = tasks.filter((task) => task.completed).length;
  return { all: tasks.length, active: tasks.length - completed, completed };
}

function visibleTasks() {
  if (activeFilter === "active") {
    return tasks.filter((task) => !task.completed);
  }
  if (activeFilter === "completed") {
    return tasks.filter((task) => task.completed);
  }
  return tasks;
}

function emptyMessage() {
  if (activeFilter === "active") {
    return "没有未完成任务，今天推进得很漂亮。";
  }
  if (activeFilter === "completed") {
    return "还没有已完成任务，勾选一项试试。";
  }
  return "清单还是空的，先写下第一件要推进的事。";
}

function renderTasks() {
  const counts = taskCounts();
  todoCount.textContent = `全部 ${counts.all} · 未完成 ${counts.active} · 已完成 ${counts.completed}`;

  filterButtons.forEach((button) => {
    const selected = button.dataset.filter === activeFilter;
    button.classList.toggle("is-active", selected);
    button.setAttribute("aria-pressed", String(selected));
    const count = counts[button.dataset.filter];
    button.querySelector("span").textContent = String(count);
  });

  todoList.replaceChildren();
  const filteredTasks = visibleTasks();
  if (filteredTasks.length === 0) {
    const empty = document.createElement("li");
    empty.className = "todo-empty";
    empty.textContent = emptyMessage();
    todoList.append(empty);
    return;
  }

  filteredTasks.forEach((task) => {
    const item = document.createElement("li");
    item.className = "todo-item";
    item.dataset.completed = String(task.completed);

    const checkbox = document.createElement("input");
    checkbox.className = "todo-checkbox";
    checkbox.type = "checkbox";
    checkbox.checked = task.completed;
    checkbox.disabled = requestInProgress;
    checkbox.setAttribute("aria-label", `${task.completed ? "恢复" : "完成"}任务：${task.title}`);
    checkbox.addEventListener("change", () => updateTask(task, checkbox.checked));

    const copy = document.createElement("span");
    copy.className = "todo-item-copy";
    const title = document.createElement("strong");
    title.textContent = task.title;
    const state = document.createElement("small");
    state.textContent = task.completed ? "已完成 · 再次勾选可恢复" : "未完成 · 勾选即完成";
    copy.append(title, state);
    item.append(checkbox, copy);
    todoList.append(item);
  });
}

async function loadTasks({ quiet = false } = {}) {
  if (!quiet) {
    setTodoStatus("正在从本地 SQLite 读取任务…", "loading");
  }
  setRequestInProgress(true);
  try {
    const payload = await requestJSON("/api/tasks");
    if (!Array.isArray(payload.tasks)) {
      throw new Error("后端返回了无法识别的任务列表");
    }
    tasks = payload.tasks;
    renderTasks();
    setTodoStatus("已与本地服务同步。刷新页面也能重新读取。", "success");
  } catch (error) {
    tasks = [];
    renderTasks();
    setTodoStatus(`无法读取任务：${error.message}`, "error");
  } finally {
    setRequestInProgress(false);
    renderTasks();
  }
}

async function updateTask(task, completed) {
  setRequestInProgress(true);
  setTodoStatus(completed ? "正在标记完成…" : "正在恢复为未完成…", "loading");
  try {
    const payload = await requestJSON(`/api/tasks/${task.id}`, {
      method: "PATCH",
      body: JSON.stringify({ completed }),
    });
    tasks = tasks.map((item) => (item.id === task.id ? payload.task : item));
    setTodoStatus(completed ? "任务已完成并保存。" : "任务已恢复为未完成。", "success");
  } catch (error) {
    setTodoStatus(`无法更新任务：${error.message}`, "error");
  } finally {
    setRequestInProgress(false);
    renderTasks();
  }
}

todoForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const title = todoInput.value.trim();
  if (!title || requestInProgress) {
    setTodoStatus("请先输入任务标题。", "error");
    todoInput.focus();
    return;
  }

  setRequestInProgress(true);
  setTodoStatus("正在保存新任务…", "loading");
  try {
    const payload = await requestJSON("/api/tasks", {
      method: "POST",
      body: JSON.stringify({ title }),
    });
    tasks.unshift(payload.task);
    todoInput.value = "";
    activeFilter = "all";
    setTodoStatus("任务已新增到服务端清单。", "success");
  } catch (error) {
    setTodoStatus(`无法新增任务：${error.message}`, "error");
  } finally {
    setRequestInProgress(false);
    renderTasks();
    todoInput.focus();
  }
});

filterButtons.forEach((button) => {
  button.addEventListener("click", () => {
    activeFilter = button.dataset.filter;
    renderTasks();
  });
});

renderTasks();
loadTasks();
