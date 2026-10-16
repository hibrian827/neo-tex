" Keep VimTeX's project/output discovery, but route viewing through WSL.
function! vimtex#view#texbridge#new() abort
  return s:viewer.init()
endfunction
let s:viewer = vimtex#view#_template#new({'name': 'texbridge'})
function! s:viewer.out() dict abort
  if exists('b:texbuild') && !has_key(b:texbuild, 'error')
    return b:texbuild.pdf
  endif
  return b:vimtex.compiler.get_file('pdf')
endfunction
function! s:viewer._check() abort
  return executable('tex-view')
endfunction
function! s:viewer._start(file) dict abort
  call luaeval("require('neo_tex').view(_A)", a:file)
endfunction
