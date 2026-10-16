local M = {}
local pending_builds = {}
local building = {}
local custom_active = {}
local queued = {}
local closing = false

local function project(file)
  local path = vim.fs.find(".texbuild.json", { path = vim.fs.dirname(file), upward = true })[1]
  if not path then return nil end
  local ok, config = pcall(function()
    local value = vim.json.decode(table.concat(vim.fn.readfile(path), "\n"))
    for _, field in ipairs({ "main", "command", "pdf" }) do
      assert(type(value[field]) == "string" and value[field]:match("%S"), field .. " must be a nonempty string")
    end
    local root = vim.fs.dirname(path)
    for _, field in ipairs({ "main", "pdf" }) do
      assert(not vim.startswith(value[field], "/"), field .. " must be relative to the configuration directory")
      value[field] = vim.fs.normalize(root .. "/" .. value[field])
      assert(vim.startswith(value[field], root .. "/"), field .. " must stay inside the project directory")
    end
    assert(vim.fn.filereadable(value.main) == 1, "main file does not exist: " .. value.main)
    value.root = root
    return value
  end)
  if not ok then return { error = path .. ": " .. tostring(config) } end
  return config
end

function M.prepare()
  local config = project(vim.api.nvim_buf_get_name(0))
  vim.b.texbuild = config
  if config then
    if config.error then
      vim.schedule(function() vim.notify(config.error, vim.log.levels.ERROR) end)
    else
      vim.b.vimtex_main = config.main
    end
  end
end

function M.compiler_method(main)
  local config = project(main)
  if not config then return "latexmk" end
  vim.g.vimtex_compiler_generic = {
    command = config.error and "false" or ("cd " .. vim.fn.shellescape(config.root) .. " && " .. config.command),
  }
  return "generic"
end

local function queue_build(buf)
  local state = vim.b[buf].vimtex
  if not state then return end
  local main = vim.fs.normalize(state.tex)
  if queued[main] then return end
  queued[main] = true
  local function run()
    if closing or not custom_active[main] then queued[main] = nil; return end
    -- A chapter can be closed while its project's build is queued.
    local target
    for _, candidate in ipairs(vim.api.nvim_list_bufs()) do
      local current = vim.b[candidate].vimtex
      if vim.api.nvim_buf_is_loaded(candidate) and current and vim.fs.normalize(current.tex) == main then
        target = candidate; break
      end
    end
    if not target then queued[main] = nil; return end
    vim.api.nvim_buf_call(target, function()
      if building[main] or vim.fn.eval("b:vimtex.compiler.is_running() ? 1 : 0") == 1 then
        vim.defer_fn(run, 100)
      else
        queued[main] = nil
        vim.fn["vimtex#compiler#start"]()
      end
    end)
  end
  vim.defer_fn(run, 100)
end

function M.setup()
  vim.api.nvim_create_user_command("Pdf", function() M.open() end,
    { desc = "Save the LaTeX project, compile, and open its PDF", force = true })
  -- Neovim user-command names require an uppercase first letter. Only expand
  -- the exact interactive :pdf command, never text in searches or arguments.
  vim.cmd([[cnoreabbrev <expr> pdf getcmdtype() ==# ':' && getcmdline() ==# 'pdf' ? 'Pdf' : 'pdf']])
  vim.g.vimtex_view_method = "texbridge"
  vim.g.vimtex_view_automatic = 0
  vim.cmd([[function! TexBuildCompiler(main) abort
    return luaeval("require('tex_workflow').compiler_method(_A)", a:main)
  endfunction]])
  vim.g.vimtex_compiler_method = "TexBuildCompiler"
  vim.g.vimtex_compiler_latexmk = { continuous = 1 }
  local group = vim.api.nvim_create_augroup("TexPreviewReconnect", { clear = true })
  vim.api.nvim_create_autocmd({ "BufReadPre", "BufNewFile" }, {
    group = group, pattern = "*.tex", callback = M.prepare,
  })
  vim.api.nvim_create_autocmd("User", {
    group = group,
    pattern = "VimtexEventInitPost",
    callback = function()
      local buf = vim.api.nvim_get_current_buf()
      if vim.b.texbuild then
        vim.api.nvim_buf_create_user_command(buf, "VimtexStop", function()
          if vim.b.vimtex then custom_active[vim.fs.normalize(vim.b.vimtex.tex)] = nil end
          vim.fn["vimtex#compiler#stop"]()
        end, {})
        vim.api.nvim_buf_create_user_command(buf, "VimtexStopAll", function()
          custom_active = {}
          vim.fn["vimtex#compiler#stop_all"]()
        end, {})
      end
      vim.schedule(function()
        if vim.api.nvim_buf_is_valid(buf) then
          vim.api.nvim_buf_call(buf, function() M.resume() end)
        end
      end)
    end,
  })
  vim.api.nvim_create_autocmd("BufWritePost", {
    group = group, pattern = { "*.tex", "*.bib", "*.sty", "*.cls" }, callback = function() M.track_save() end,
  })
  vim.api.nvim_create_autocmd("User", {
    group = group, pattern = "VimtexEventCompileStarted", callback = function()
      if vim.b.texbuild and not vim.b.texbuild.error and vim.b.vimtex then
        custom_active[vim.fs.normalize(vim.b.vimtex.tex)] = true
      end
    end,
  })
  vim.api.nvim_create_autocmd("VimLeavePre", {
    group = group, callback = function() M.finish() end,
  })
end

local function start_compiler()
  if vim.b.vimtex and building[vim.fs.normalize(vim.b.vimtex.tex)] then return end
  if vim.b.texbuild then
    if not vim.b.texbuild.error then custom_active[vim.fs.normalize(vim.b.vimtex.tex)] = true end
    return
  end
  if vim.fn.eval("b:vimtex.compiler.is_running() ? 1 : 0") == 0 then
    vim.fn["vimtex#compiler#start"]()
  end
end

local function request_data(pdf)
  return {
    pdf = vim.fn.fnamemodify(pdf, ":p"),
    root = vim.b.texbuild and vim.b.texbuild.root or vim.b.vimtex.root,
    source = vim.api.nvim_buf_get_name(0),
    line = vim.api.nvim_win_get_cursor(0)[1],
    server = vim.v.servername,
    remote = vim.env.SSH_CONNECTION ~= nil,
    tmux = vim.env.TMUX,
    pane = vim.env.TMUX_PANE,
  }
end

local function build_command()
  return vim.fn.eval("extend(copy(b:vimtex.compiler), {'continuous': 0, 'callback': 0}).__build_cmd('')")
end

function M.open()
  if not vim.b.vimtex then
    vim.notify(":pdf requires a LaTeX source buffer", vim.log.levels.ERROR)
    return
  end
  if vim.b.texbuild and vim.b.texbuild.error then
    vim.notify(vim.b.texbuild.error, vim.log.levels.ERROR)
    return
  end
  local main = vim.fs.normalize(vim.b.vimtex.tex)
  local root = vim.fs.normalize(vim.b.texbuild and vim.b.texbuild.root or vim.b.vimtex.root)
  if building[main] then return end
  local command = build_command()
  building[main] = true
  local was_running = vim.fn.eval("b:vimtex.compiler.is_running() ? 1 : 0") == 1
  local job = vim.fn.eval("get(b:vimtex.compiler, 'job', -1)")
  if was_running then
    vim.fn["vimtex#compiler#stop"]()
    if type(job) == "number" and job > 0 then vim.fn.jobwait({ job }, 2000) end
  end
  local saved, error = pcall(function()
    for _, buf in ipairs(vim.api.nvim_list_bufs()) do
      if vim.api.nvim_buf_is_loaded(buf) and vim.bo[buf].modified and vim.bo[buf].buftype == "" then
        local name = vim.fs.normalize(vim.api.nvim_buf_get_name(buf))
        local state = vim.b[buf].vimtex
        local belongs = state and vim.fs.normalize(state.tex) == main
        local extra = name:sub(1, #root + 1) == root .. "/" and
          vim.tbl_contains({ "bib", "sty", "cls" }, vim.fn.fnamemodify(name, ":e"))
        if belongs or extra then
          vim.api.nvim_buf_call(buf, function() vim.cmd("update") end)
        end
      end
    end
  end)
  if not saved then
    building[main] = nil
    if was_running then start_compiler() end
    vim.notify("PDF preview cancelled: " .. tostring(error), vim.log.levels.ERROR)
    return
  end
  local result = vim.system({ "/bin/sh", "-c", command }, { cwd = root, text = true }):wait(120000)
  building[main] = nil
  if result.code ~= 0 then
    local folder = vim.fn.expand("~/.cache/tex-view")
    vim.fn.mkdir(folder, "p")
    local log = folder .. "/build-" .. vim.fn.sha256(main):sub(1, 24) .. ".log"
    vim.fn.writefile(vim.split((result.stdout or "") .. (result.stderr or ""), "\n"), log)
    if was_running then start_compiler() end
    vim.notify("PDF preview cancelled: compilation failed. See " .. log, vim.log.levels.ERROR)
    return
  end
  vim.cmd("VimtexView")
end

function M.track_save()
  if not vim.b.vimtex then
    -- Bibliography/style buffers do not have a VimTeX state of their own.
    local file = vim.api.nvim_buf_get_name(0)
    for _, buf in ipairs(vim.api.nvim_list_bufs()) do
      local config = vim.b[buf].texbuild
      if config and not config.error and vim.b[buf].vimtex and vim.startswith(file, config.root .. "/") then
        vim.api.nvim_buf_call(buf, M.track_save)
        return
      end
    end
    return
  end
  if vim.b.texbuild and vim.b.texbuild.error then return end
  local request = request_data(vim.fn.eval("b:vimtex.viewer.out()"))
  -- Use VimTeX's own engine/options/output paths, with continuous mode disabled.
  request.build_command = build_command()
  pending_builds[request.pdf] = request
  if vim.b.texbuild and custom_active[vim.fs.normalize(vim.b.vimtex.tex)] and
      not building[vim.fs.normalize(vim.b.vimtex.tex)] then
    queue_build(vim.api.nvim_get_current_buf())
  end
end

function M.finish()
  closing = true
  if next(pending_builds) == nil then return end
  local jobs = vim.fn.eval("map(vimtex#state#list_all(), {_, s -> get(get(s, 'compiler', {}), 'job', -1)})")
  vim.fn["vimtex#compiler#stop_all"]()
  jobs = vim.tbl_filter(function(job) return type(job) == "number" and job > 0 end, jobs)
  if #jobs > 0 then vim.fn.jobwait(jobs, 2000) end
  for _, request in pairs(pending_builds) do
    local result = vim.system({ "tex-view", "finish" }, { stdin = vim.json.encode(request), text = true }):wait(125000)
    if result.code ~= 0 then
      vim.notify("Final LaTeX build failed; see ~/.cache/tex-view/final-build-*.log", vim.log.levels.ERROR)
    end
  end
  pending_builds = {}
end

function M.resume()
  if vim.b.texbuild and vim.b.texbuild.error then return end
  if not vim.b.vimtex or vim.fn.eval('get(b:vimtex, "tex_view_attach_checked", 0)') == 1 then
    return
  end
  vim.cmd("let b:vimtex.tex_view_attach_checked = 1")
  local buf = vim.api.nvim_get_current_buf()
  local state = vim.b.vimtex.id
  local request = request_data(vim.fn.eval("b:vimtex.viewer.out()"))
  vim.system({ "tex-view", "resume" }, { stdin = vim.json.encode(request), text = true }, function(result)
    vim.schedule(function()
      if result.code == 0 and vim.trim(result.stdout) == "1" and vim.api.nvim_buf_is_valid(buf) then
        vim.api.nvim_buf_call(buf, function()
          if vim.b.vimtex and vim.b.vimtex.id == state then
            start_compiler()
            if vim.b.texbuild then queue_build(buf) end
          end
        end)
      end
    end)
  end)
end

function M.view(pdf)
  -- Viewing starts the live-preview workflow without toggling a running job off.
  start_compiler()
  local request = request_data(pdf)
  vim.system({ "tex-view", "request" }, { stdin = vim.json.encode(request), text = true }, function(result)
    vim.schedule(function()
      if result.code ~= 0 then
        vim.notify(result.stderr, vim.log.levels.ERROR)
      elseif request.remote then
        vim.notify("PDF requested in WSL. Connect with tex-ssh or run tex-view bridge SERVER in WSL.")
      end
    end)
  end)
end

-- Called through the specific editor's Unix socket, never an exposed TCP port.
function M.inverse(args)
  local file, line = args[1], tonumber(args[2]) or 1
  local buf = vim.fn.bufnr(file)
  local win = buf >= 0 and vim.fn.bufwinid(buf) or -1
  if win >= 0 then
    vim.api.nvim_set_current_win(win)
  else
    -- Keep navigation in one editing pane. Hiding preserves unsaved source
    -- buffers; deleting a chapter buffer then cannot leave a duplicate split.
    vim.cmd("hide edit " .. vim.fn.fnameescape(file))
  end
  vim.api.nvim_win_set_cursor(0, { math.max(1, math.min(line, vim.api.nvim_buf_line_count(0))), 0 })
  vim.cmd("normal! zz")
  return true
end

return M
