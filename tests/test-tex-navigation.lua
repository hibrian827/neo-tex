-- Run from the repository root: nvim --headless -u NONE -l tests/test-tex-navigation.lua
local tex = dofile("nvim/lua/tex_workflow/init.lua")
local root = vim.fn.tempname()
vim.fn.mkdir(root, "p")
local main = root .. "/main.tex"
local chapter = root .. "/chapter with spaces.tex"
local results = root .. "/results.tex"
for _, file in ipairs({ main, chapter, results }) do
  vim.fn.writefile({ "first line", "second line", "third line" }, file)
end

local ok, err = pcall(function()
  local views = 0
  local original_open = tex.open
  tex.open = function() views = views + 1 end
  tex.setup()
  vim.api.nvim_feedkeys(vim.api.nvim_replace_termcodes(":pdf<CR>", true, false, true), "xt", false)
  assert(views == 1, "lowercase :pdf did not dispatch to the PDF viewer")
  vim.api.nvim_feedkeys(vim.api.nvim_replace_termcodes(":let g:pdf_test_text = 'pdf'<CR>", true, false, true), "xt", false)
  assert(vim.g.pdf_test_text == "pdf", "PDF abbreviation changed text in another command")
  tex.open = original_open
  vim.o.hidden = false
  vim.cmd.edit(main)
  local original = vim.api.nvim_get_current_buf()
  vim.api.nvim_buf_set_lines(original, 0, 1, false, { "unsaved original edit" })
  tex.inverse({ chapter, 2 })
  assert(#vim.api.nvim_list_wins() == 1, "navigation created an extra pane")
  assert(vim.api.nvim_buf_get_name(0) == chapter)
  assert(vim.api.nvim_win_get_cursor(0)[1] == 2)
  assert(vim.bo[original].modified, "navigation discarded the original unsaved edit")
  assert(vim.api.nvim_buf_get_lines(original, 0, 1, false)[1] == "unsaved original edit")
  local chapter_buf = vim.api.nvim_get_current_buf()
  tex.inverse({ results, 3 })
  vim.cmd.bdelete()
  vim.cmd.bdelete(chapter_buf)
  assert(#vim.api.nvim_list_wins() == 1, "closing chapter buffers left duplicate panes")
  assert(vim.api.nvim_get_current_buf() == original, "closing chapters did not return to the original")

  -- User-created splits remain intact, and a visible target reuses its pane.
  vim.cmd("sbuffer " .. chapter_buf) -- bdelete leaves a reusable unloaded buffer.
  local target_win = vim.api.nvim_get_current_win()
  vim.cmd.wincmd("p")
  local before = #vim.api.nvim_list_wins()
  tex.inverse({ chapter, 2 })
  assert(vim.api.nvim_get_current_win() == target_win, "visible source pane was not reused")
  assert(#vim.api.nvim_list_wins() == before, "navigation changed user-created splits")
end)
vim.fn.delete(root, "rf")
if not ok then error(err) end
print("PASS PDF navigation reuses panes, preserves unsaved edits, and closes chapters without duplicate splits")
