---@diagnostic disable: undefined-field, missing-fields
local fff_rust = require('fff.rust')
local fuzzy = require('fff.fuzzy')

local function git(dir, args)
  local cmd = { 'git', '-C', dir, '-c', 'user.name=t', '-c', 'user.email=t@t' }
  vim.list_extend(cmd, args)
  local out = vim.fn.system(cmd)
  assert(vim.v.shell_error == 0, table.concat(cmd, ' ') .. ' failed: ' .. out)
end

describe('linked git worktree history', function()
  local tmp, main, worktree

  local function write(path, content)
    vim.fn.mkdir(vim.fn.fnamemodify(path, ':h'), 'p')
    local f = assert(io.open(path, 'w'))
    f:write(content)
    f:close()
  end

  local function find(name)
    local result = fuzzy.fuzzy_search_files('', 4, nil, 100, 3, 0, 50)
    for _, item in ipairs(result.items) do
      if item.name == name then return item end
    end
  end

  local function wait_for_access_score(name, expected)
    return vim.wait(10000, function()
      local item = find(name)
      return item ~= nil and item.access_frecency_score == expected
    end, 25)
  end

  local function wait_for_access_count(path, expected)
    return vim.wait(10000, function() return fff_rust.get_file_access_count(path) == expected end, 25)
  end

  before_each(function()
    pcall(fff_rust.stop_background_monitor)
    pcall(fff_rust.cleanup_file_picker)

    tmp = vim.fn.tempname()
    vim.fn.mkdir(tmp, 'p')
    tmp = vim.fn.resolve(vim.fn.fnamemodify(tmp, ':p')):gsub('/+$', '')
    main = tmp .. '/main'
    worktree = tmp .. '/feature'

    write(main .. '/hot.lua', 'return 1')
    write(main .. '/cold.lua', 'return 1')
    git(main, { 'init', '-b', 'main' })
    git(main, { 'add', '-A' })
    git(main, { 'commit', '-m', 'seed', '--no-gpg-sign' })
    git(main, { 'worktree', 'add', worktree, '-b', 'feature' })

    assert.is_true(fff_rust.init_db(tmp .. '/frecency', tmp .. '/history', true))
  end)

  after_each(function()
    pcall(fff_rust.stop_background_monitor)
    pcall(fff_rust.cleanup_file_picker)
    pcall(fff_rust.destroy_frecency_db)
    pcall(fff_rust.destroy_query_db)
    if tmp then vim.fn.delete(tmp, 'rf') end
  end)

  it('starts from the main checkout frecency and feeds it back', function()
    fff_rust.track_access(main .. '/hot.lua')
    assert.is_true(wait_for_access_count(main .. '/hot.lua', 1))

    assert.is_true(fff_rust.init_file_picker(worktree))
    fff_rust.wait_for_initial_scan(30000)
    assert.is_true(wait_for_access_score('hot.lua', 1), 'worktree did not inherit main history')

    fff_rust.track_access(worktree .. '/cold.lua')
    assert.is_true(wait_for_access_count(main .. '/cold.lua', 1), 'worktree open was not shared')
    assert.is_true(wait_for_access_score('cold.lua', 2), 'worktree open should weigh double locally')
  end)

  it('shares query history with the main checkout', function()
    assert.is_true(fff_rust.init_file_picker(worktree))
    fff_rust.wait_for_initial_scan(30000)
    fff_rust.track_query_completion('cold', worktree .. '/cold.lua')
    assert.is_true(vim.wait(10000, function() return fff_rust.get_historical_query(0) == 'cold' end, 25))

    pcall(fff_rust.stop_background_monitor)
    fff_rust.cleanup_file_picker()
    assert.is_true(fff_rust.init_file_picker(main))
    fff_rust.wait_for_initial_scan(30000)
    assert.are.equal('cold', fff_rust.get_historical_query(0))
  end)
end)
