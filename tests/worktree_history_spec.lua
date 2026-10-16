---@diagnostic disable: undefined-field, missing-fields
local rust = require('fff.rust')

local function git(dir, args)
  local cmd = { 'git', '-C', dir, '-c', 'user.name=t', '-c', 'user.email=t@t' }
  vim.list_extend(cmd, args)
  local out = vim.fn.system(cmd)
  assert(vim.v.shell_error == 0, table.concat(cmd, ' ') .. ' failed: ' .. out)
end

local function switch_picker(path)
  pcall(rust.stop_background_monitor)
  pcall(rust.cleanup_file_picker)
  assert.is_true(rust.init_file_picker(path))
  assert.is_true(rust.wait_for_initial_scan(10000))
end

local function find(name)
  local result = rust.fuzzy_search_files('lua', 4, nil, 100, 3, 0, 10)
  for i, item in ipairs(result.items) do
    if item.name == name then return item, result.scores[i] end
  end
  error(name .. ' is missing from search results')
end

local function wait_for(predicate) assert.is_true(vim.wait(10000, predicate, 10), 'background update timed out') end

describe('linked git worktree history', function()
  local tmp, main, worktree, sibling

  before_each(function()
    pcall(rust.stop_background_monitor)
    pcall(rust.cleanup_file_picker)
    tmp = vim.fn.tempname()
    vim.fn.mkdir(tmp .. '/main', 'p')
    tmp = vim.fn.resolve(vim.fn.fnamemodify(tmp, ':p')):gsub('/+$', '')
    main, worktree, sibling = tmp .. '/main', tmp .. '/feature', tmp .. '/other'
    vim.fn.writefile({ 'return 1' }, main .. '/hot.lua')
    vim.fn.writefile({ 'return 1' }, main .. '/cold.lua')
    git(main, { 'init', '-b', 'main' })
    git(main, { 'add', '-A' })
    git(main, { 'commit', '-m', 'seed', '--no-gpg-sign' })
    git(main, { 'worktree', 'add', worktree, '-b', 'feature' })
    git(main, { 'worktree', 'add', sibling, '-b', 'other' })
    assert.is_true(rust.init_db(tmp .. '/frecency', tmp .. '/history', true))
  end)

  after_each(function()
    pcall(rust.stop_background_monitor)
    pcall(rust.cleanup_file_picker)
    pcall(rust.destroy_frecency_db)
    pcall(rust.destroy_query_db)
    if tmp then vim.fn.delete(tmp, 'rf') end
  end)

  it('shares accesses and keeps the extra boost local', function()
    switch_picker(main)
    rust.track_access(main .. '/hot.lua')
    wait_for(function() return find('hot.lua').access_frecency_score == 1 end)

    switch_picker(worktree)
    assert.are.equal(1, find('hot.lua').access_frecency_score)
    rust.track_access(worktree .. '/hot.lua')
    wait_for(function() return find('hot.lua').access_frecency_score == 3 end)
    assert.are.equal(2, rust.get_file_access_count(worktree .. '/hot.lua'))

    for _, base in ipairs({ sibling, main }) do
      switch_picker(base)
      assert.are.equal(2, find('hot.lua').access_frecency_score)
      assert.are.equal(0, find('cold.lua').access_frecency_score)
    end
  end)

  it('preserves combos across switches and resets them for a different file', function()
    for count, base in ipairs({ main, main, main, worktree, sibling, main }) do
      switch_picker(base)
      rust.track_query_completion('lua', base .. '/hot.lua')
      wait_for(function() return rust.get_historical_query(count - 1) == 'lua' end)
      local _, score = find('hot.lua')
      assert.are.equal(count >= 3 and count * 100 or 0, score.combo_match_boost)
    end

    rust.track_query_completion('lua', main .. '/cold.lua')
    wait_for(function() return rust.get_historical_query(6) == 'lua' end)
    for _, name in ipairs({ 'hot.lua', 'cold.lua' }) do
      local _, score = find(name)
      assert.are.equal(0, score.combo_match_boost)
    end
  end)

  it('watches the linked worktree index when staging a file', function()
    switch_picker(worktree)
    vim.fn.writefile({ 'return 2' }, worktree .. '/hot.lua')
    wait_for(function() return find('hot.lua').git_status == 'modified' end)
    git(worktree, { 'add', 'hot.lua' })
    wait_for(function() return find('hot.lua').git_status == 'staged_modified' end)
  end)
end)
